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
from typing import Any

from backend.core.digest import content_fingerprint
from backend.repositories.document_repo import DocumentRepository

logger = logging.getLogger(__name__)

# 产物依赖链：下游由上游正文推导而来，上游**重新生成**后下游即失效。
# 只对「整篇重生成」生效（首次生成 / 重新生成）；`optimize`（就地优化）与
# `clarify`（按澄清改写）都是对**同一份**文档的局部重写，不牵动下游。
# 「提示词由接口文档推出？还是由 PRD 推出？」——现有链路是 PRD → 接口文档 → 提示词，
# 因此重生成 PRD 会让两份下游都失效，重生成接口文档只会让提示词失效。
DOWNSTREAM_KINDS: dict[str, tuple[str, ...]] = {
    "prd": ("api_docs", "prompts"),
    "api_docs": ("prompts",),
    "prompts": (),
}


# ---------------------------------------------------------------------------
# 读：当前文档
# ---------------------------------------------------------------------------


def list_documents(owner_id: int, solution_id: int) -> list[dict[str, Any]]:
    """列出某方案下的全部文档；方案不存在或不属于该用户时返回空列表。"""
    return [_to_item(row) for row in DocumentRepository.list_by_solution(owner_id, solution_id)]


def get_document(owner_id: int, solution_id: int, kind: str) -> dict[str, Any] | None:
    """读取一份文档；不存在或不属于该用户时返回 None（由路由层翻成 404）。"""
    row = DocumentRepository.get(owner_id, solution_id, kind)
    return _to_item(row) if row else None


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

    Returns:
        更新后的文档；方案不可见时返回 None。
    """
    source = source or kind
    pending_json = json.dumps(pending_items or [], ensure_ascii=False)
    summary_json = json.dumps(run_summary, ensure_ascii=False) if run_summary is not None else None
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
                invalidate=DOWNSTREAM_KINDS.get(kind, ()),
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
# 裁剪 / 解析
# ---------------------------------------------------------------------------


def _to_item(row: dict[str, Any]) -> dict[str, Any]:
    """把「文档行 + 当前版本行」的合并行裁成对外结构，并把两个 JSON 文本列解成对象。

    解析一律容错（脏数据退化为空清单 / 空回执）：这两个列是**展示用**的附加信息，
    读不动它不该让整份文档读不出来——正文才是主角。
    """
    return {
        "kind": row["kind"],
        "content": row["content"] or "",
        "pending_items": _load_json(row["pending_items"], list, []),
        "last_run_summary": _load_json(row["last_run_summary"], dict, None),
        "updated_at": row["updated_at"],
    }


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
