"""文档（documents）业务服务。

三份产物正文（PRD / 接口文档 / 提示词）在后端的一等实体。归属仍由 `solutions` 决定，
这里只做「按方案取文档」与「写文档」两类事——不做权限判断（仓储层已经保证），
也不管 HTTP（路由层负责把 `None` 翻成 404）。

正文只住在版本表（`document_versions`）里；`documents` 一行表示「这份文档的身份 + 它
当前指向哪一版」。对外结构（`_to_item`）与版本化之前**逐字段一致**，因此读文档的所有
调用方（路由、前端）无需改动。

**四种写入语义，不要混用**：

- `save_document`：**用户手改**。就地改当前版本的正文，不新增版本条目；
  `pending_items` 与 `last_run_summary` 原样保留——清单是模型产出的元数据，
  用户改几个字不构成「清单过期」。
- `commit_generated(append_version=True)`：**整篇生成**。追加一条新版本，并让下游产物
  失效（见 `DOWNSTREAM_KINDS`）；正文 + 清单 + 回执同批写入新版本。
- `commit_generated(append_version=False)`：**`optimize` / `clarify` 的就地改写**。
  就地改当前版本，不新增条目、不删下游；但正文 + 清单 + 回执仍是同批替换——
  它们产自同一次模型调用栈，分开写就会各自漂移。
- `checkpoint` / `restore`：用户主动「存为新版本」/「恢复某一版」。两者都**追加新版本**。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from backend.core.digest import content_fingerprint
from backend.repositories.document_repo import DocumentRepository

logger = logging.getLogger(__name__)

# 产物依赖链：下游由上游正文推导而来。
#
# ⚠️ 这张表现在**只用于说明依赖关系**：上游整篇重生成时**不再删除下游**。
# 删除会让下游的历史与澄清线程一起消失，用户无法判断“旧的那份哪里不对”；
# 改为在读取时比较 `metadata.derived_from` 记录的上游内容指纹，不一致就给前端
# 一个 `stale` 标记（黄条提示），由用户决定重新生成还是继续用。
# 「提示词由接口文档推出？还是由 PRD 推出？」——现有链路是 PRD → 接口文档 → 提示词。
DOWNSTREAM_KINDS: dict[str, tuple[str, ...]] = {
    "prd": ("api_docs", "prompts"),
    "api_docs": ("prompts",),
    "prompts": (),
}

# 删除版本的结果。**机器判据**，路由据此翻成 HTTP 状态码，不拿中文文案判断：
# - `ok`          已删除
# - `missing`     版本不存在，或不属于当前用户（两者对外不可区分）
# - `is_current`  想删的是当前版本 —— 它不是「历史」
DeleteVersionOutcome = Literal["ok", "missing", "is_current"]


# ---------------------------------------------------------------------------
# 读：当前文档
# ---------------------------------------------------------------------------


def list_documents(owner_id: int, solution_id: int) -> list[dict[str, Any]]:
    """列出某方案下的全部文档；方案不存在或不属于该用户时返回空列表。

    先取全部三份的当前 `content_sha` 建映射，再逐条算 `stale`：下游是否过期取决于
    **上游现在的内容**，单看一行无法判断。
    """
    rows = DocumentRepository.list_by_solution(owner_id, solution_id)
    sha_by_kind = {row["kind"]: (row.get("content_sha") or "") for row in rows}
    return [_to_item(row, sha_by_kind) for row in rows]


def get_document(owner_id: int, solution_id: int, kind: str) -> dict[str, Any] | None:
    """读取一份文档；不存在或不属于该用户时返回 None（由路由层翻成 404）。"""
    rows = DocumentRepository.list_by_solution(owner_id, solution_id)
    sha_by_kind = {row["kind"]: (row.get("content_sha") or "") for row in rows}
    row = next((item for item in rows if item["kind"] == kind), None)
    return _to_item(row, sha_by_kind) if row else None


def current_version_meta(owner_id: int, solution_id: int, kind: str) -> dict[str, Any] | None:
    """取某份文档当前版本的「版本号 + 内容指纹」，供下游记录 `derived_from`。

    只读两个字段：调用方（生成收尾）不需要正文本身，只需要一个能在日后比较的锚点。
    文档不存在时返回 None，调用方据此不写 `derived_from`（而不是写一个空锚点）。
    """
    row = DocumentRepository.get(owner_id, solution_id, kind)
    if not row:
        return None
    return {
        "version_no": int(row.get("version_no") or 0),
        "content_sha": row.get("content_sha") or "",
    }


# ---------------------------------------------------------------------------
# 写：手改 / 生成收尾
# ---------------------------------------------------------------------------


def save_document(
    owner_id: int, solution_id: int, kind: str, content: str
) -> dict[str, Any] | None:
    """保存用户手改后的正文（就地改当前版本），返回更新后的文档；方案不可见时返回 None。"""
    ok = DocumentRepository.update_current_version(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        content=content,
        content_sha=content_fingerprint(content),
    )
    if not ok:
        return None
    return get_document(owner_id, solution_id, kind)


def commit_generated(
    owner_id: int,
    solution_id: int,
    kind: str,
    content: str,
    *,
    pending_items: list[dict[str, Any]] | None = None,
    run_summary: dict[str, Any] | None = None,
    append_version: bool = True,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """生成（或改写）收尾落稿：正文 + 待确认清单 + 用量回执一同写入。

    Args:
        owner_id: 文档所属用户。
        solution_id: 文档所属方案。
        kind: 产物类型（`prd` / `api_docs` / `prompts`）。
        content: 已剥离标记块的正文。
        pending_items: 本次产出的待确认清单；`None` 视为空清单。
        run_summary: 本次生成的用量回执；`None` 表示该版本没有回执。
        append_version: `True` 追加一条新版本并让下游失效（整篇生成）；
            `False` 就地改写当前版本、不牵动下游（`optimize` / `clarify`）。
        source: 版本来源标签；缺省取产物类型（`prd` / `api_docs` / `prompts`）。
            就地改写时由调用方显式传动作类型（`optimize` / `clarify`）。
        metadata: 版本附加信息（当前用于 `derived_from`）。**只在追加版本时生效**：
            就地改写沿用当前版本原有的 metadata，不会被覆盖。

    Returns:
        更新后的文档；方案不可见时返回 None。
    """
    source = source or kind
    pending_json = json.dumps(pending_items or [], ensure_ascii=False)
    summary_json = json.dumps(run_summary, ensure_ascii=False) if run_summary is not None else None
    metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else "{}"
    content_sha = content_fingerprint(content)

    if append_version:
        ok = (
            DocumentRepository.append_version(
                owner_id=owner_id,
                solution_id=solution_id,
                kind=kind,
                content=content,
                content_sha=content_sha,
                pending_items=pending_json,
                last_run_summary=summary_json,
                source=source,
                metadata=metadata_json,
                # 下游不再删除：上游换版后下游保留，由读取侧的 `stale` 提示用户。
                invalidate=(),
            )
            is not None
        )
    else:
        ok = DocumentRepository.replace_current_version(
            owner_id=owner_id,
            solution_id=solution_id,
            kind=kind,
            content=content,
            content_sha=content_sha,
            pending_items=pending_json,
            last_run_summary=summary_json,
            source=source,
        )
    if not ok:
        return None
    return get_document(owner_id, solution_id, kind)


# ---------------------------------------------------------------------------
# 版本链：读列表 / 读详情 / 打里程碑 / 恢复
# ---------------------------------------------------------------------------


def list_versions(owner_id: int, solution_id: int, kind: str) -> list[dict[str, Any]]:
    """列出某文档的全部版本摘要（不含正文），从新到旧。方案不可见时返回空列表。"""
    return [
        _to_version_item(row)
        for row in DocumentRepository.list_versions(owner_id, solution_id, kind)
    ]


def get_version(
    owner_id: int, solution_id: int, kind: str, version_id: int
) -> dict[str, Any] | None:
    """读取一条版本的完整内容；版本不存在或不属于该用户时返回 None（路由翻 404）。"""
    row = DocumentRepository.get_version(owner_id, solution_id, kind, version_id)
    return _to_version_detail(row) if row else None


def checkpoint(
    owner_id: int,
    solution_id: int,
    kind: str,
    content: str,
    note: str = "",
) -> dict[str, Any] | None:
    """把用户眼前的正文**存为一个新版本**（打里程碑）。

    正文取自客户端（可能比库里新——防抖窗口内用户刚敲的字还没落库，直接存库里那份会
    丢掉它）；清单与回执从**当前版本**继承——打里程碑不改变「这份文档还差哪些问题没敲定」
    这一事实。方案不可见时返回 None。
    """
    current = DocumentRepository.get(owner_id, solution_id, kind)
    pending = (current or {}).get("pending_items")
    summary = (current or {}).get("last_run_summary")
    version_id = DocumentRepository.append_version(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        content=content,
        content_sha=content_fingerprint(content),
        pending_items=pending if isinstance(pending, str) and pending else "[]",
        last_run_summary=summary if isinstance(summary, str) and summary else None,
        source="checkpoint",
        note=note,
    )
    if version_id is None:
        return None
    return get_document(owner_id, solution_id, kind)


def restore(
    owner_id: int, solution_id: int, kind: str, version_id: int
) -> dict[str, Any] | None:
    """把某一历史版本**恢复为当前版本**——做法是「读源版本 → 追加一条新版本」。

    **不是**把指针拨回旧版本：那样会丢掉「曾经恢复到哪一版」的痕迹，且旧版本一旦被
    就地改（手改/优化）就会篡改历史。追加新版本让历史只增不改，恢复动作本身也留痕。

    恢复的正文、清单、回执都取源版本——「把当时那一版（连同它的待办清单）原样搬回来」。
    源版本不存在或不属于该用户时返回 None（路由翻 404）。
    """
    source = DocumentRepository.get_version(owner_id, solution_id, kind, version_id)
    if source is None:
        return None
    new_id = DocumentRepository.append_version(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        content=source["content"] or "",
        content_sha=source["content_sha"] or "",
        pending_items=source["pending_items"] or "[]",
        last_run_summary=source["last_run_summary"],
        source="restore",
        note="",
        metadata=json.dumps({"restored_from": source["version_no"]}, ensure_ascii=False),
    )
    if new_id is None:
        return None
    return get_document(owner_id, solution_id, kind)


# ---------------------------------------------------------------------------
# 版本链：删除
# ---------------------------------------------------------------------------


def delete_version(
    owner_id: int, solution_id: int, kind: str, version_id: int
) -> DeleteVersionOutcome:
    """删除一条**历史**版本，返回机器可读的结果码（见 `DeleteVersionOutcome`）。

    **当前版本不允许删除**——它正被当作这份文档的正文在用，删掉等于把内容抽走，
    而「当前是哪一版」这个指针就会指向空。想换当前版本请用 `restore`：那是**追加**
    一条新版本，历史只增不改；「删掉当前版本」则是不可逆地抹掉一份正文，两者不是
    同一件事，不该由同一个动作承担。

    先查后删分两步，但**真正的守卫在仓储的 SQL 里**（`delete_version` 的 `WHERE`
    带了「必须不是当前版本」），因此这两步之间的时间窗不会放进一次非法删除——
    最坏情况是返回 `missing`（那条在此期间已经变了），不会误删。
    """
    row = DocumentRepository.get_version(owner_id, solution_id, kind, version_id)
    if row is None:
        return "missing"
    if row["is_current"]:
        return "is_current"
    deleted = DocumentRepository.delete_version(owner_id, solution_id, kind, version_id)
    return "ok" if deleted else "missing"


# ---------------------------------------------------------------------------
# 裁剪 / 解析
# ---------------------------------------------------------------------------


def _to_item(row: dict[str, Any], sha_by_kind: dict[str, str] | None = None) -> dict[str, Any]:
    """把「文档行 + 当前版本行」的合并行裁成对外结构，并把两个 JSON 文本列解成对象。

    解析一律容错（脏数据退化为空清单 / 空回执）：这两个列是**展示用**的附加信息，
    读不动它不该让整份文档读不出来——正文才是主角。
    `stale` 由 `metadata.derived_from` 记录的上游指纹与上游**当前**指纹比较得出：
    相等即同源，不等即“上游改过了”。没有记录（旧数据、PRD 本身）恒为 false。
    """
    metadata = _load_json(row.get("metadata"), dict, {})
    derived = metadata.get("derived_from") if isinstance(metadata, dict) else None
    if not isinstance(derived, dict):
        derived = None
    return {
        "kind": row["kind"],
        "content": row["content"] or "",
        "pending_items": _load_json(row["pending_items"], list, []),
        "last_run_summary": _load_json(row["last_run_summary"], dict, None),
        "updated_at": row["updated_at"],
        "derived_from": derived,
        "stale": _is_stale(derived, sha_by_kind or {}),
    }


def _is_stale(derived: dict[str, Any] | None, sha_by_kind: dict[str, str]) -> bool:
    """下游是否基于旧版上游：逐个上游比较 `content_sha`。"""
    if not derived:
        return False
    for upstream_kind, ref in derived.items():
        if not isinstance(ref, dict):
            continue
        recorded = str(ref.get("content_sha") or "")
        current = sha_by_kind.get(upstream_kind) or ""
        # 任一侧取不到指纹时不做判断：宁可少提示，也不要误报“过期”。
        if recorded and current and recorded != current:
            return True
    return False


def _to_version_item(row: dict[str, Any]) -> dict[str, Any]:
    """版本摘要的对外结构（**不含正文**）。"""
    return {
        "version_id": int(row["version_id"]),
        "version_no": int(row["version_no"]),
        "source": row["source"],
        "note": row["note"] or "",
        "content_sha": row["content_sha"] or "",
        "content_length": int(row["content_length"] or 0),
        "created_at": row["created_at"],
        "is_current": bool(row["is_current"]),
    }


def _to_version_detail(row: dict[str, Any]) -> dict[str, Any]:
    """版本详情的对外结构（含正文 / 清单 / 回执）。"""
    return {
        "version_id": int(row["version_id"]),
        "version_no": int(row["version_no"]),
        "content": row["content"] or "",
        "pending_items": _load_json(row["pending_items"], list, []),
        "last_run_summary": _load_json(row["last_run_summary"], dict, None),
        "source": row["source"],
        "note": row["note"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "is_current": bool(row["is_current"]),
    }


def _load_json(raw: Any, expected: type, fallback: Any) -> Any:
    """把 JSON 文本解析成 `expected` 类型；任何异常都退化为 `fallback`。"""
    if not isinstance(raw, str) or not raw.strip():
        return fallback
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("文档字段不是合法 JSON，已按空值处理", extra={"event": "document.bad_json"})
        return fallback
    return parsed if isinstance(parsed, expected) else fallback
