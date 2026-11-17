"""文档（documents）业务服务。

三份产物正文（PRD / 接口文档 / 提示词）在后端的一等实体。归属仍由 `solutions` 决定，
这里只做「按方案取文档」与「写文档」两类事——不做权限判断（仓储层已经保证），
也不管 HTTP（路由层负责把 `None` 翻成 404）。

正文只住在版本表（`document_versions`）里；`documents` 一行表示「这份文档的身份 + 它
当前指向哪一版」。对外结构（`_to_item`）与版本化之前**逐字段一致**，因此读文档的所有
调用方（路由、前端）无需改动。

**正文写入只有一个入口（票 #81）**：

- `write_document(mode=...)`：手改 / 整篇生成 / 修复 / 恢复 / 就地改写**全部**走它，
  实现只有一份（见 `DocumentWriteMode`）。带 `expected_version_no` 时做版本锁：
  调用方「我以为当前是第几版」与库里对不上就返回 `version_conflict`、一个字都不写。
  这三种语义分别是：
  - `append`：**整篇生成 / 修复 / 存为新版本 / 恢复**。追加一条新版本，正文 + 清单 +
    回执 + **审核结论**同批写入新版本；审核试过但没跑成时同批写 `review_failed_at`。
  - `replace`：**`optimize` / `clarify` 的就地改写**。就地改当前版本，不新增条目、不删
    下游；但正文 + 清单 + 回执仍是同批替换（它们产自同一次模型调用栈）。
  - `manual`：**用户手改**。就地改当前版本正文，`pending_items` 与 `last_run_summary`
    原样保留——清单是模型产出的元数据，用户改几个字不构成「清单过期」。
- `checkpoint` / `restore` 是 `write_document(mode="append")` 的薄封装；
  `commit_generated` / `save_document` 同理（保留旧形状给非收尾调用方）。
- `commit_review`：**重新审核**的收尾。只更新当前版本的 `review` 列，
  不追加版本、不改正文、不动清单与回执（ADR 0002 第 2 条）。它不是「写正文」，
  但可以写进调用方的事务（收尾把结论与任务终态 / 账本收在同一个事务里）。

## 事务（v6 / T16.7；#81 扩展为可入外部事务）

每个对外函数用**一个** `session_scope()` 包住整次操作：读写落在同一个 session 里，
不再出现「写完关连接、再开连接读回」的中间态。仓储返回模型对象/投影，
本层把它们翻成对外的 `dict`（路由与前端契约不变）。

写正文与写结论的入口还接受 `session`（#81）：收尾（`_finalize`）持有事务，把
「宣布成功 / 记一条账 / 写正文 / 写结论」收进同一个事务；给了 session 时本层
**不提交、不回滚**，事务边界仍只在调用方。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from backend.core.clock import utc_now_text
from backend.core.digest import content_fingerprint
from backend.core.engine import session_scope
from backend.core.limits import ensure_text_within_limit
from backend.repositories.document_repo import (
    DocumentRepository,
    DocumentRow,
    VersionDetail,
    VersionSummary,
)

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


def _read_document(
    session: Any, owner_id: int, solution_id: int, kind: str
) -> dict[str, Any] | None:
    """在同一 session 里读回一份文档（列表 + 裁剪），供写操作收尾复用。

    单取一份也走 `list_by_solution`：`stale` 判定需要**全部**三份的当前指纹，
    只查一份拿不到上游的内容指纹。
    """
    rows = DocumentRepository(session).list_by_solution(owner_id, solution_id)
    sha_by_kind = {row.kind: (row.content_sha or "") for row in rows}
    row = next((item for item in rows if item.kind == kind), None)
    return _to_item(row, sha_by_kind) if row else None


# ---------------------------------------------------------------------------
# 读：当前文档
# ---------------------------------------------------------------------------


def list_documents(owner_id: int, solution_id: int) -> list[dict[str, Any]]:
    """列出某方案下的全部文档；方案不存在或不属于该用户时返回空列表。

    先取全部三份的当前 `content_sha` 建映射，再逐条算 `stale`：下游是否过期取决于
    **上游现在的内容**，单看一行无法判断。
    """
    with session_scope() as session:
        rows = DocumentRepository(session).list_by_solution(owner_id, solution_id)
        sha_by_kind = {row.kind: (row.content_sha or "") for row in rows}
        return [_to_item(row, sha_by_kind) for row in rows]


def get_document(owner_id: int, solution_id: int, kind: str) -> dict[str, Any] | None:
    """读取一份文档；不存在或不属于该用户时返回 None（由路由层翻成 404）。"""
    with session_scope() as session:
        return _read_document(session, owner_id, solution_id, kind)


def current_version_meta(
    owner_id: int,
    solution_id: int,
    kind: str,
    *,
    session: Any | None = None,
) -> dict[str, Any] | None:
    """取某份文档当前版本的「版本号 + 内容指纹」，供下游记录 `derived_from`。

    只读两个字段：调用方（生成收尾）不需要正文本身，只需要一个能在日后比较的锚点。
    文档不存在时返回 None，调用方据此不写 `derived_from`（而不是写一个空锚点）。

    `session` 给定时在调用方事务里读（收尾算派生锚点用的就是同一个快照，不再另开
    一次连接看到可能已经变过的库状态）。
    """
    if session is None:
        with session_scope() as own_session:
            row = DocumentRepository(own_session).get(owner_id, solution_id, kind)
    else:
        row = DocumentRepository(session).get(owner_id, solution_id, kind)
    if not row:
        return None
    return {
        "version_no": int(row.version_no or 0),
        "content_sha": row.content_sha or "",
    }


# ---------------------------------------------------------------------------
# 写：**唯一**的正文写入入口（手改 / 整篇生成 / 修复 / 恢复 / 就地改写）
# ---------------------------------------------------------------------------

# 一次正文写入的语义。三条都是「把一份正文写进 documents」，区别只在写进哪一版：
# - `append`  追加一条新版本并前移当前版本指针（整篇生成 / 修复 / 恢复 / 存为新版本）
# - `replace` 就地改当前版本，正文 + 清单 + 回执**同批替换**（optimize / clarify）
# - `manual`  就地改当前版本，**只**动正文（用户手改；清单与回执原样保留）
DocumentWriteMode = Literal["append", "replace", "manual"]

# 一次正文写入的结果。**机器判据**，调用方据此决定界面与任务终态，不拿「0 行 / None」猜：
# - `ok`             写进去了
# - `version_conflict` 版本号对不上（调用方以为的当前版本已不是当前版本）——一个字都没写
# - `missing`        方案不可见 / 目标不存在（这次生成没有地方可落）
DocumentWriteOutcome = Literal["ok", "version_conflict", "missing"]

# 版本锁拒绝时给用户看的那句话（任务终态 error_message 与 HTTP 409 的 detail 共用一份）：
# 手改 / 就地改写撞上「当前版本已经不是我以为那一版」时，覆盖回去会把别人的新正文抹掉，
# 正确处置是拒绝写入并让用户刷新后重试。前端 `stores/documents.ts` 原样展示这一句。
VERSION_CONFLICT_MESSAGE = "正文已被更新，请刷新后重试"


@dataclass(frozen=True, slots=True)
class DocumentWriteResult:
    """统一写入入口的返回值：结果码 + （写成时的）读回文档。"""

    outcome: DocumentWriteOutcome
    document: dict[str, Any] | None = None


def write_document(
    owner_id: int,
    solution_id: int,
    kind: str,
    content: str,
    *,
    mode: DocumentWriteMode,
    expected_version_no: int | None = None,
    pending_items: list[dict[str, Any]] | None = None,
    run_summary: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
    review_failed_at: str | None = None,
    source: str | None = None,
    note: str = "",
    metadata: dict[str, Any] | None = None,
    session: Any | None = None,
) -> DocumentWriteResult:
    """**正文写入的唯一入口**（票 #81）：手改 / 整篇生成 / 修复 / 恢复 / 就地改写都走这里。

    ## 为什么收成一个入口

    从前有四条各写一遍的路径：`save_document`、`commit_generated(append_version=True/False)`、
    `checkpoint`、`restore`。每一条自己决定「追加还是就地改、带不带清单 / 回执 / 结论」，
    于是「哪一版是当前版本」这个判据只有三条腿中的一条知道。收成一个带 `mode` 与
    `expected_version_no` 的入口之后：写入语义只剩一份实现，版本锁只有一处，测试也只需要
    盯一处。

    ## 版本锁

    `expected_version_no` 是调用方「我以为当前是第几版」（用 `documents.version_no` 表示）。
    给出时**对不上就拒绝**（返回 `version_conflict`，一个字都不写）——这是「手改与生成互相
    覆盖」的结构性修复：用户在版本 1 上编辑、生成期间追加了版本 2，手改的自动保存会带着
    版本 1 发出去，必须在写之前被拒，而不是按「当前版本」盲改。
    `None` 表示调用方不持版本期望（整篇生成 / 恢复这类追加写入本来就不覆盖当前正文）。

    ## 事务

    `session` 为空时本函数自己开一个 `session_scope()`（非收尾调用方的便捷形态）；
    给了 session 就**不提交、不回滚**，写入落在调用方的事务里——收尾（`_finalize`）用它把
    「宣布成功 / 记一条账 / 写正文 / 写结论」收进同一个事务。版本冲突的判定发生在任何写入
    之前（先读当前版本号再动笔），因此外部事务里返回 `version_conflict` 时库里什么都没变，
    调用方可以直接据此把这次收尾整批回滚并按失败处理。

    Args:
        mode: 见 `DocumentWriteMode`。
        expected_version_no: 「我以为当前是第几版」；`None` = 不做版本比对。
            0 表示「我认为还没有任何版本」（首次手改时允许顺势建版本 1）。
        pending_items / run_summary: 本次产出的待确认清单与用量回执；`manual` 模式忽略。
        review: 这一版正文的审核结论（只与 `append` 有关：结论描述的就是这一版正文，
            与正文同批写入新版本）。
        review_failed_at: 「这次审核试过但没跑成」的时间；只与 `append` 有关。
        source: 版本来源标签；缺省 `append` 取产物类型、`replace`/`manual` 取 `manual`。
        note / metadata: 版本备注与附加信息（`checkpoint` / `restore` 用）。
        session: 调用方持有的 session；为空时本函数自开事务。
    """
    if session is None:
        with session_scope() as own_session:
            return _write_document_in(
                own_session,
                owner_id,
                solution_id,
                kind,
                content,
                mode=mode,
                expected_version_no=expected_version_no,
                pending_items=pending_items,
                run_summary=run_summary,
                review=review,
                review_failed_at=review_failed_at,
                source=source,
                note=note,
                metadata=metadata,
            )
    return _write_document_in(
        session,
        owner_id,
        solution_id,
        kind,
        content,
        mode=mode,
        expected_version_no=expected_version_no,
        pending_items=pending_items,
        run_summary=run_summary,
        review=review,
        review_failed_at=review_failed_at,
        source=source,
        note=note,
        metadata=metadata,
    )


def _write_document_in(
    session: Any,
    owner_id: int,
    solution_id: int,
    kind: str,
    content: str,
    *,
    mode: DocumentWriteMode,
    expected_version_no: int | None,
    pending_items: list[dict[str, Any]] | None,
    run_summary: dict[str, Any] | None,
    review: dict[str, Any] | None,
    review_failed_at: str | None,
    source: str | None,
    note: str,
    metadata: dict[str, Any] | None,
) -> DocumentWriteResult:
    """统一写入的实现（在给定 session 里跑，不碰事务边界）。

    三件事必须在这里**一次**做完，不许散到各调用方：

    1. **版本锁**：先读当前版本号。期望对不上就返回 `version_conflict`，此时库里一个字
       都还没写——这正是它能被收尾事务当成「可回滚的拒绝」用的前提。
    2. **写**：`append` 走 `append_version`；`replace` / `manual` 走
       `write_current_version`（后者把「当前版本号 == 期望」放进 SQL 条件，兜住第一步
       之后到写入之前的时间窗）。
    3. **读回**：同一个 session 读回对外结构（路由与调用方契约不变）。
    """
    content_sha = content_fingerprint(content)
    # 上限口径对所有写入一致：以前只有追加版本那条走闸门，就地改写能绕过它
    # （优化稿超 60KB 会直接写进库）。统一入口之后不再有第二条路。
    ensure_text_within_limit(content, field="文档正文")

    repo = DocumentRepository(session)
    existing = repo.get(owner_id, solution_id, kind)
    current_no = 0 if existing is None or existing.version_no is None else int(existing.version_no)
    if expected_version_no is not None and current_no != expected_version_no:
        return DocumentWriteResult(outcome="version_conflict")

    # 缺省来源标签：追加版本记「产物类型」（prd / api_docs / prompts），就地改写记动作
    # （调用方显式给 optimize / clarify / manual）。
    version_source = source or (kind if mode == "append" else "manual")
    pending_json = json.dumps(pending_items or [], ensure_ascii=False)
    summary_json = (
        json.dumps(run_summary, ensure_ascii=False) if run_summary is not None else None
    )
    metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else "{}"
    review_json = json.dumps(review, ensure_ascii=False) if review is not None else None

    if mode == "append":
        version = repo.append_version(
            owner_id=owner_id,
            solution_id=solution_id,
            kind=kind,
            content=content,
            content_sha=content_sha,
            pending_items=pending_json,
            last_run_summary=summary_json,
            review=review_json,
            review_failed_at=review_failed_at,
            source=version_source,
            note=note,
            metadata=metadata_json,
            # 下游不再删除：上游换版后下游保留，由读取侧的 `stale` 提示用户。
            invalidate=(),
        )
        if version is None:
            return DocumentWriteResult(outcome="missing")
    else:
        revision = repo.write_current_version(
            owner_id=owner_id,
            solution_id=solution_id,
            kind=kind,
            content=content,
            content_sha=content_sha,
            pending_items=pending_json if mode == "replace" else None,
            last_run_summary=summary_json if mode == "replace" else None,
            replace_generated_meta=mode == "replace",
            source=version_source,
            expected_version_no=expected_version_no,
        )
        if revision == "version_conflict":
            return DocumentWriteResult(outcome="version_conflict")
        if revision == "missing":
            return DocumentWriteResult(outcome="missing")

    return DocumentWriteResult(
        outcome="ok", document=_read_document(session, owner_id, solution_id, kind)
    )


def save_document(
    owner_id: int,
    solution_id: int,
    kind: str,
    content: str,
    *,
    expected_version_no: int | None = None,
    session: Any | None = None,
) -> dict[str, Any] | None:
    """保存用户手改后的正文（就地改当前版本），返回更新后的文档；方案不可见时返回 None。

    薄封装：写入实现只有 `write_document` 一份。需要分辨「版本冲突」的调用方
    （保存路由）直接用 `write_document` 拿机器判据；这里保持既有的 `dict | None` 形状。
    """
    result = write_document(
        owner_id,
        solution_id,
        kind,
        content,
        mode="manual",
        expected_version_no=expected_version_no,
        session=session,
    )
    return result.document


def commit_generated(
    owner_id: int,
    solution_id: int,
    kind: str,
    content: str,
    *,
    pending_items: list[dict[str, Any]] | None = None,
    run_summary: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
    review_failed_at: str | None = None,
    append_version: bool = True,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
    expected_version_no: int | None = None,
    session: Any | None = None,
) -> dict[str, Any] | None:
    """生成（或改写）收尾落稿：正文 + 待确认清单 + 用量回执一同写入。

    薄封装：`append_version=True` → `write_document(mode="append")`；
    `False` → `write_document(mode="replace")`。**写入实现只有一份**，收尾（`_finalize`）
    直接调 `write_document` 并把它放进自己的事务里（本封装保留给非收尾调用方与既有用例）。
    """
    result = write_document(
        owner_id,
        solution_id,
        kind,
        content,
        mode="append" if append_version else "replace",
        expected_version_no=expected_version_no,
        pending_items=pending_items,
        run_summary=run_summary,
        review=review,
        review_failed_at=review_failed_at,
        source=source,
        metadata=metadata,
        session=session,
    )
    return result.document



# ---------------------------------------------------------------------------
# 版本链：读列表 / 读详情 / 打里程碑 / 恢复
# ---------------------------------------------------------------------------


# 「重新审核」收尾写入的结果。**机器判据**，任务层据此决定是成功还是失败：
# - `ok`              已写入当前版本的 `review` 列
# - `missing`         这份文档不存在 / 方案不可见 / 还没有任何正文
# - `content_changed` 当前版本的正文已经不是「被审的那一份」（指纹不一致）——不写
ReviewCommitOutcome = Literal["ok", "missing", "content_changed"]


def commit_review(
    owner_id: int,
    solution_id: int,
    kind: str,
    review: dict[str, Any],
    *,
    expected_content_sha: str | None = None,
    session: Any | None = None,
) -> ReviewCommitOutcome:
    """**重新审核**收尾：只更新当前版本的审核结论（不追加版本、不动正文）。

    为什么单独一个入口而不是复用 `commit_generated`：后者的语义是「写正文」——它要么追加
    版本，要么就地重写正文，两条路都会改变「这一版是什么」。重审只刷新这一版的一条判定，
    正文与清单一个字都不该动（ADR 0002 第 2 条）。

    `expected_content_sha` 是**被审那一份正文**的指纹（建任务那一刻快照的）。给出时走
    条件写：当前版本的指纹与它不一致就**一个字都不写**，返回 `content_changed`。
    这道闸门的理由见 §3.8：结论是「这一版被审成什么样」的记录，写到一个没被审过的
    版本上不会报错，只会变成一条看着正常、实则指向别的正文的假记录。

    `session` 给定时不提交、不回滚：收尾（`_finalize`）用它把结论与任务终态 / 账本收进
    同一个事务（#81 的 AC4：收尾后账本、正文、结论属于同一版本）。

    Args:
        owner_id: 文档所属用户。
        solution_id: 文档所属方案。
        kind: 产物类型（`prd` / `api_docs` / `prompts`）。
        review: 本次结论（形状与 SSE `review` 事件的数据一致，见 `document_review.review_data`）。
        expected_content_sha: 被审正文的内容指纹；`None` 表示不做比对（仅供不关心
            「写之前正文是否变过」的内部调用）。
        session: 调用方持有的事务；为空时本函数自开一个短事务。

    Returns:
        见 `ReviewCommitOutcome`；没有正文就没有「被审的那一版」，写一条无主结论只会
        让读侧以为审过（`missing`）。
    """
    review_json = json.dumps(review, ensure_ascii=False)
    if session is None:
        with session_scope() as own_session:
            return _commit_review_in(
                own_session,
                owner_id,
                solution_id,
                kind,
                review_json,
                expected_content_sha=expected_content_sha,
            )
    return _commit_review_in(
        session,
        owner_id,
        solution_id,
        kind,
        review_json,
        expected_content_sha=expected_content_sha,
    )


def _commit_review_in(
    session: Any,
    owner_id: int,
    solution_id: int,
    kind: str,
    review_json: str,
    *,
    expected_content_sha: str | None,
) -> ReviewCommitOutcome:
    """`commit_review` 的实现（在给定 session 里跑；不碰事务边界）。"""
    repo = DocumentRepository(session)
    ok = repo.replace_current_review(
        owner_id,
        solution_id,
        kind,
        review=review_json,
        expected_content_sha=expected_content_sha,
    )
    if ok:
        return "ok"
    # 没能写入：区分「根本没有这一版」与「有，但正文已经换了」
    row = repo.get(owner_id, solution_id, kind)
    if row is None or row.current_version_id is None:
        return "missing"
    return "content_changed"


def mark_review_failed(
    owner_id: int,
    solution_id: int,
    kind: str,
    *,
    session: Any | None = None,
) -> bool:
    """把「这次重审没跑成」记到当前版本上（时间戳），返回是否写进去了。

    为什么这条事实要落库：重审的唯一产出就是判定，而「没审成」不是「没审过」——
    只说在会话内存里的话，刷新之后界面会退化成「未审核」，用户再也看不出这份正文
    被审过、且没审成（US3）。拿不到当前版本时返回 `False`（没有「哪一版失败」可言）。

    与结论同一行：失败描述的就是这一版；`commit_review` 写入成功时会把它清空。
    `session` 给定时写进调用方的事务（收尾失败那一笔与任务终态 / 账本同事务，#81）。
    """
    if session is None:
        with session_scope() as own_session:
            return DocumentRepository(own_session).mark_review_failed(
                owner_id, solution_id, kind, at=utc_now_text()
            )
    return DocumentRepository(session).mark_review_failed(
        owner_id, solution_id, kind, at=utc_now_text()
    )


def list_versions(owner_id: int, solution_id: int, kind: str) -> list[dict[str, Any]]:
    """列出某文档的全部版本摘要（不含正文），从新到旧。方案不可见时返回空列表。"""
    with session_scope() as session:
        rows = DocumentRepository(session).list_versions(owner_id, solution_id, kind)
        return [_to_version_item(row) for row in rows]


def get_version(
    owner_id: int, solution_id: int, kind: str, version_id: int
) -> dict[str, Any] | None:
    """读取一条版本的完整内容；版本不存在或不属于该用户时返回 None（路由翻 404）。"""
    with session_scope() as session:
        row = DocumentRepository(session).get_version(owner_id, solution_id, kind, version_id)
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

    写入实现只有一份：读当前版本（继承清单 / 回执）与追加版本**在同一个事务里**，
    由统一入口 `write_document(mode="append")` 完成。
    """
    with session_scope() as session:
        current = DocumentRepository(session).get(owner_id, solution_id, kind)
        pending = _load_json(current.pending_items if current else None, list, [])
        summary = _load_json(current.last_run_summary if current else None, dict, None)
        result = write_document(
            owner_id,
            solution_id,
            kind,
            content,
            mode="append",
            pending_items=pending,
            run_summary=summary,
            source="checkpoint",
            note=note,
            session=session,
        )
        return result.document


def restore(
    owner_id: int, solution_id: int, kind: str, version_id: int
) -> dict[str, Any] | None:
    """把某一历史版本**恢复为当前版本**——做法是「读源版本 → 追加一条新版本」。

    **不是**把指针拨回旧版本：那样会丢掉「曾经恢复到哪一版」的痕迹，且旧版本一旦被
    就地改（手改/优化）就会篡改历史。追加新版本让历史只增不改，恢复动作本身也留痕。

    恢复的正文、清单、回执都取源版本——「把当时那一版（连同它的待办清单）原样搬回来」。
    源版本不存在或不属于该用户时返回 None（路由翻 404）。读源版本与追加版本在同一个
    事务里，写入仍走唯一入口 `write_document(mode="append")`。
    """
    with session_scope() as session:
        source = DocumentRepository(session).get_version(
            owner_id, solution_id, kind, version_id
        )
        if source is None:
            return None
        result = write_document(
            owner_id,
            solution_id,
            kind,
            source.content or "",
            mode="append",
            pending_items=_load_json(source.pending_items, list, []),
            run_summary=_load_json(source.last_run_summary, dict, None),
            source="restore",
            note="",
            metadata={"restored_from": source.version_no},
            session=session,
        )
        return result.document


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
    with session_scope() as session:
        row = DocumentRepository(session).get_version(
            owner_id, solution_id, kind, version_id
        )
        if row is None:
            return "missing"
        if row.is_current:
            return "is_current"
        deleted = DocumentRepository(session).delete_version(
            owner_id, solution_id, kind, version_id
        )
        return "ok" if deleted else "missing"


# ---------------------------------------------------------------------------
# 裁剪 / 解析
# ---------------------------------------------------------------------------


def _to_item(row: DocumentRow, sha_by_kind: dict[str, str] | None = None) -> dict[str, Any]:
    """把「文档行 + 当前版本行」的合并投影裁成对外结构，并把三个 JSON 文本列解成对象。

    解析一律容错（脏数据退化为空清单 / 空回执 / 无结论）：这些列是**展示用**的附加
    信息，读不动它不该让整份文档读不出来——正文才是主角。
    `review` 是当前版本那一条审核结论（一版一条）；`None` 表示这一版没审过 / 没审成，
    界面据此显示「未审核 / 审核失败」，绝不替它猜一个「通过」。
    `stale` 由 `metadata.derived_from` 记录的上游指纹与上游**当前**指纹比较得出：
    相等即同源，不等即“上游改过了”。没有记录（旧数据、PRD 本身）恒为 false。
    """
    metadata = _load_json(row.metadata, dict, {})
    derived = metadata.get("derived_from") if isinstance(metadata, dict) else None
    if not isinstance(derived, dict):
        derived = None
    return {
        "kind": row.kind,
        "content": row.content or "",
        "pending_items": _load_json(row.pending_items, list, []),
        "last_run_summary": _load_json(row.last_run_summary, dict, None),
        "review": _load_json(row.review, dict, None),
        # 「这一版上一次审核试过但没跑成」的时间；`None` = 没有失败记录（不是「没审过」）
        "review_failed_at": row.review_failed_at,
        # 当前版本序号：结论面板据此说清「这条结论是哪一版的」（后端已有这一列，
        # 只是在版本行里、从前没随文档下发）。没有版本时是 0。
        "version_no": int(row.version_no or 0),
        "updated_at": row.updated_at,
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


def _to_version_item(row: VersionSummary) -> dict[str, Any]:
    """版本摘要的对外结构（**不含正文**）。"""
    return {
        "version_id": int(row.version_id),
        "version_no": int(row.version_no),
        "source": row.source,
        "note": row.note or "",
        "content_sha": row.content_sha or "",
        "content_length": int(row.content_length or 0),
        "created_at": row.created_at,
        "is_current": bool(row.is_current),
    }


def _to_version_detail(row: VersionDetail) -> dict[str, Any]:
    """版本详情的对外结构（含正文 / 清单 / 回执）。"""
    return {
        "version_id": int(row.version_id),
        "version_no": int(row.version_no),
        "content": row.content or "",
        "pending_items": _load_json(row.pending_items, list, []),
        "last_run_summary": _load_json(row.last_run_summary, dict, None),
        "review": _load_json(row.review, dict, None),
        "review_failed_at": row.review_failed_at,
        "source": row.source,
        "note": row.note or "",
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "is_current": bool(row.is_current),
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
