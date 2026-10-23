"""文档（documents）与文档版本（document_versions）的数据仓储。

与 `solution_repo.py` 同一套约定：**每个用户面方法的首参都是 `owner_id`，且不接受
`None`**——没有「不过滤归属」的逃生口。

与 `solutions` 不同的地方在于：`documents` / `document_versions` 都没有 `owner_id`
列。归属的唯一来源是它们所属的 `solutions.user_id`，于是这里用 `JOIN solutions`
把越权行在 SQL 层面滤掉，路由层只需把「查不到 / 没写进去」翻译成 404。冗余一个
`owner_id` 会让归属出现第二个真源，两边迟早不一致。

版本模型下的两处关键约定：

- **正文只住在 `document_versions`**。`documents` 一行只表示「这份文档的身份 + 它当前
  指向哪一版」（`current_version_id`）。读「当前文档」= 文档行 JOIN 当前版本行。
- **写入一律在单个事务里完成**：`append_version` 的「删下游 → 保证文档行 → 插版本 →
  前移指针」四步必须同生共死，否则会留下「指针指向没写进去的版本」这类半成品。
  事务边界由调用方的 `session_scope()` 给出，本模块**不 commit / rollback / close**。

## 形态（v6 / T16.7）

绑定 `Session` 的实例方法：`DocumentRepository(session).get(...)`。

单表读取返回 ORM 模型对象；跨表 JOIN 读取返回**只读投影**（`DocumentRow` /
`VersionSummary` / `VersionDetail`）。为什么不是 `dict`、也不是模型对象：ADR-078 要求
不返回 `dict`（属性写错要等运行时才暴露），而 ADR-080 又刻意不声明 `Relationship()`
（懒加载会在 session 关闭后炸），所以 JOIN 的结果没有单一 ORM 对象可承载——用冻结
dataclass 做投影，字段与列一一对应，既拿到属性访问与类型检查，又不把大字段（正文）
拖进「不含正文」的版本列表。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from backend.core.clock import utc_now_text
from backend.core.models import Document, DocumentVersion, Solution


# ---------------------------------------------------------------------------
# 只读投影：JOIN 结果没有单一 ORM 对象可承载（ADR-080 不声明 Relationship）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentRow:
    """「文档行 + 当前版本行」的合并投影。字段与 `_merged_select` 的标签一一对应。"""

    document_id: int
    solution_id: int
    kind: str
    created_at: str
    updated_at: str
    current_version_id: int | None
    version_id: int | None
    version_no: int | None
    content: str | None
    content_sha: str | None
    pending_items: str | None
    last_run_summary: str | None
    source: str | None
    metadata: str | None


@dataclass(frozen=True, slots=True)
class VersionSummary:
    """版本列表项（**不含正文**）。"""

    version_id: int
    version_no: int
    source: str
    note: str | None
    content_sha: str | None
    content_length: int
    created_at: str
    updated_at: str
    is_current: bool


@dataclass(frozen=True, slots=True)
class VersionDetail:
    """版本详情（含正文 / 清单 / 回执）。"""

    version_id: int
    version_no: int
    content: str | None
    content_sha: str | None
    pending_items: str | None
    last_run_summary: str | None
    source: str
    note: str | None
    metadata: str | None
    created_at: str
    updated_at: str
    is_current: bool


def _merged_select():
    """「文档行 + 当前版本行 + 归属」的合并取数，对外读文档的口径全走它。

    `LEFT JOIN document_versions`（当前版本可能尚未建立），`JOIN solutions`（归属闸门）。
    `DocumentVersion.meta` 的列名是 `metadata`，这里显式 `label("metadata")`，
    保证投影字段名与旧 SQL 的别名一致。
    """
    return (
        select(
            Document.id.label("document_id"),
            Document.solution_id.label("solution_id"),
            Document.kind.label("kind"),
            Document.created_at.label("created_at"),
            Document.updated_at.label("updated_at"),
            Document.current_version_id.label("current_version_id"),
            DocumentVersion.id.label("version_id"),
            DocumentVersion.version_no.label("version_no"),
            DocumentVersion.content.label("content"),
            DocumentVersion.content_sha.label("content_sha"),
            DocumentVersion.pending_items.label("pending_items"),
            DocumentVersion.last_run_summary.label("last_run_summary"),
            DocumentVersion.source.label("source"),
            DocumentVersion.meta.label("metadata"),
        )
        .select_from(Document)
        .join(DocumentVersion, DocumentVersion.id == Document.current_version_id, isouter=True)
        .join(Solution, Solution.id == Document.solution_id)
    )


class DocumentRepository:
    """`documents` / `document_versions` 的读写。所有方法强制带 `owner_id`。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # 读：当前文档
    # ------------------------------------------------------------------

    def list_by_solution(self, owner_id: int, solution_id: int) -> list[DocumentRow]:
        """列出某方案下的全部文档（至多三份）连同各自的当前版本，按写入先后排序。"""
        rows = self.session.execute(
            _merged_select()
            .where(Solution.user_id == owner_id, Document.solution_id == solution_id)
            .order_by(Document.id)
        ).mappings().all()
        return [DocumentRow(**dict(row)) for row in rows]

    def get(self, owner_id: int, solution_id: int, kind: str) -> DocumentRow | None:
        """读取一份文档（含当前版本）；不存在、或方案不属于该用户时返回 None。"""
        row = self.session.execute(
            _merged_select().where(
                Solution.user_id == owner_id,
                Document.solution_id == solution_id,
                Document.kind == kind,
            )
        ).mappings().first()
        return DocumentRow(**dict(row)) if row else None

    # ------------------------------------------------------------------
    # 读：版本链
    # ------------------------------------------------------------------

    def list_versions(
        self, owner_id: int, solution_id: int, kind: str
    ) -> list[VersionSummary]:
        """列出某文档的全部版本摘要（**不带正文**），按版本号从新到旧。

        返回值含 `is_current`（该版本是否当前版本）。正文只在取单个版本详情时读——
        版本列表可能很长，把正文都拖出来是纯浪费。
        """
        rows = self.session.execute(
            select(
                DocumentVersion.id.label("version_id"),
                DocumentVersion.version_no.label("version_no"),
                DocumentVersion.source.label("source"),
                DocumentVersion.note.label("note"),
                DocumentVersion.content_sha.label("content_sha"),
                func.length(DocumentVersion.content).label("content_length"),
                DocumentVersion.created_at.label("created_at"),
                DocumentVersion.updated_at.label("updated_at"),
                (DocumentVersion.id == Document.current_version_id).label("is_current"),
            )
            .select_from(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Solution, Solution.id == Document.solution_id)
            .where(
                Solution.user_id == owner_id,
                Document.solution_id == solution_id,
                Document.kind == kind,
            )
            .order_by(DocumentVersion.version_no.desc())
        ).mappings().all()
        return [
            VersionSummary(
                version_id=int(row["version_id"]),
                version_no=int(row["version_no"]),
                source=row["source"],
                note=row["note"],
                content_sha=row["content_sha"],
                content_length=int(row["content_length"] or 0),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                is_current=bool(row["is_current"]),
            )
            for row in rows
        ]

    def get_version(
        self, owner_id: int, solution_id: int, kind: str, version_id: int
    ) -> VersionDetail | None:
        """读取一条版本的完整内容（含正文 / 清单 / 回执）；不属于该用户时返回 None。"""
        row = self.session.execute(
            select(
                DocumentVersion.id.label("version_id"),
                DocumentVersion.version_no.label("version_no"),
                DocumentVersion.content.label("content"),
                DocumentVersion.content_sha.label("content_sha"),
                DocumentVersion.pending_items.label("pending_items"),
                DocumentVersion.last_run_summary.label("last_run_summary"),
                DocumentVersion.source.label("source"),
                DocumentVersion.note.label("note"),
                DocumentVersion.meta.label("metadata"),
                DocumentVersion.created_at.label("created_at"),
                DocumentVersion.updated_at.label("updated_at"),
                (DocumentVersion.id == Document.current_version_id).label("is_current"),
            )
            .select_from(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Solution, Solution.id == Document.solution_id)
            .where(
                Solution.user_id == owner_id,
                Document.solution_id == solution_id,
                Document.kind == kind,
                DocumentVersion.id == version_id,
            )
        ).mappings().first()
        if row is None:
            return None
        return VersionDetail(
            version_id=int(row["version_id"]),
            version_no=int(row["version_no"]),
            content=row["content"],
            content_sha=row["content_sha"],
            pending_items=row["pending_items"],
            last_run_summary=row["last_run_summary"],
            source=row["source"],
            note=row["note"],
            metadata=row["metadata"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            is_current=bool(row["is_current"]),
        )

    # ------------------------------------------------------------------
    # 写：删除历史版本 / 追加版本 / 就地改当前版本
    # ------------------------------------------------------------------

    def delete_version(
        self, owner_id: int, solution_id: int, kind: str, version_id: int
    ) -> bool:
        """删除一条**历史**版本，删掉了返回 True。

        ⚠️ **当前版本删不得**，而且这条限制写在 SQL 的 WHERE 里（`id != COALESCE(
        current_version_id, -1)`）而不是「先查一次、再决定删不删」：两次查询之间若发生
        别的写入（例如另一个标签页刚好把它恢复成当前版本），先查后删就会放行——那时
        删掉的是文档正在用的正文。把判据和动作收在同一条语句里，这个窗口就不存在。

        归属仍经 `documents → solutions` 两级 JOIN 过滤，越权与不存在一样「删不掉」。

        删除**历史**版本不影响当前正文，因此这里刻意不去动 `documents.updated_at`——
        它表示「这份文档的当前内容什么时候变的」，删一条旧版并没有改变这件事。
        """
        owned_doc_ids = (
            select(Document.id)
            .join(Solution, Solution.id == Document.solution_id)
            .where(
                Solution.user_id == owner_id,
                Document.solution_id == solution_id,
                Document.kind == kind,
            )
        )
        current_of_doc = (
            select(Document.current_version_id)
            .where(Document.id == DocumentVersion.document_id)
            .scalar_subquery()
        )
        result = self.session.execute(
            delete(DocumentVersion).where(
                DocumentVersion.id == version_id,
                DocumentVersion.document_id.in_(owned_doc_ids),
                DocumentVersion.id != func.coalesce(current_of_doc, -1),
            )
        )
        return int(result.rowcount or 0) == 1

    def append_version(
        self,
        owner_id: int,
        solution_id: int,
        kind: str,
        *,
        content: str,
        content_sha: str,
        pending_items: str,
        last_run_summary: str | None,
        source: str,
        note: str = "",
        metadata: str = "{}",
        invalidate: tuple[str, ...] = (),
    ) -> DocumentVersion | None:
        """追加一条新版本并把它设为当前版本；返回新版本**模型对象**，方案不可见时返回 None。

        `invalidate` 列出「被本次生成取代、应当删除」的**产物种类**（下游产物从上游正文
        推导而来，上游重写后它们就失效了）。删除与追加**在同一事务里**完成：若分成两次
        提交，中间那一瞬就是「新正文已到、旧下游还在」，刷新页面会看到一份自相矛盾的方案。
        删下游文档会**级联删掉它们的版本**，无需另写删版本语句。

        四步顺序不能变（环形外键 `documents.current_version_id ↔ document_versions.document_id`
        决定的）：删下游 → **先空指针插入文档行** → 插版本行 → 再回填指针。反过来会撞上
        「指针指向还不存在的版本」。归属不通过时第二步一行都插不进去、第三步查不到行，
        于是整体回滚返回 None。

        全程**不提交**：提交由调用方 `session_scope()` 在四步都成功之后一次性完成，
        因此任何一步抛异常都不会留下「新版本已写、指针没动」的中间态。
        """
        now = utc_now_text()

        # 第一步：删下游（同事务；归属条件防止改到别人的方案）
        if invalidate:
            owned_solution_ids = select(Solution.id).where(
                Solution.id == solution_id, Solution.user_id == owner_id
            )
            self.session.execute(
                delete(Document).where(
                    Document.solution_id == solution_id,
                    Document.kind.in_(list(invalidate)),
                    Document.solution_id.in_(owned_solution_ids),
                )
            )

        # 第二步：确保文档行存在（指针留空）
        document = self._ensure_document(owner_id, solution_id, kind, now)
        if document is None:
            return None

        # 第三步：插版本行（flush 拿自增 id）
        next_no = int(
            self.session.execute(
                select(func.coalesce(func.max(DocumentVersion.version_no), 0) + 1).where(
                    DocumentVersion.document_id == document.id
                )
            ).scalar_one()
        )
        version = DocumentVersion(
            document_id=document.id,
            version_no=next_no,
            content=content,
            content_sha=content_sha,
            pending_items=pending_items,
            last_run_summary=last_run_summary,
            source=source,
            note=note,
            meta=metadata,
            created_at=now,
            updated_at=now,
        )
        self.session.add(version)
        self.session.flush()

        # 第四步：回填当前版本指针
        document.current_version_id = version.id
        document.updated_at = now
        self.session.flush()
        return version

    def update_current_version(
        self,
        owner_id: int,
        solution_id: int,
        kind: str,
        *,
        content: str,
        content_sha: str,
    ) -> bool:
        """**就地**改写当前版本的正文，成功返回 True（不新增版本条目）。

        用于用户手改：只动正文，该版本上的 `pending_items` / `last_run_summary` 原样保留
        ——清单是模型产出的元数据，用户改几个字不构成「清单过期」。

        文档行还不存在时（手改先于任何生成）会顺势建行 + 建 `version_no=1, source='manual'`，
        保持「没有生成也能手改」这一既有语义。方案不可见时返回 False。
        """
        return self._update_current_version(
            owner_id,
            solution_id,
            kind,
            content=content,
            content_sha=content_sha,
            pending_items=None,
            last_run_summary=None,
            replace_generated_meta=False,
            source="manual",
        )

    def replace_current_version(
        self,
        owner_id: int,
        solution_id: int,
        kind: str,
        *,
        content: str,
        content_sha: str,
        pending_items: str,
        last_run_summary: str | None,
        source: str = "manual",
    ) -> bool:
        """**就地**改写当前版本，且正文 + 清单 + 回执**同批替换**（不新增版本条目）。

        用于 `optimize` / `clarify`：这两者是对**同一份**文档的局部重写，不牵动下游，
        但那三者产自同一次模型调用栈，必须整批落——分开写就会出现「清单与正文对不上」。

        `source` 只在「当前版本还不存在、需要顺势建 `version_no=1`」这条兜底路径上用到。
        """
        return self._update_current_version(
            owner_id,
            solution_id,
            kind,
            content=content,
            content_sha=content_sha,
            pending_items=pending_items,
            last_run_summary=last_run_summary,
            replace_generated_meta=True,
            source=source,
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _ensure_document(
        self, owner_id: int, solution_id: int, kind: str, now: str
    ) -> Document | None:
        """确保 `documents` 行存在并返回它；方案不属于该用户时返回 None。

        用 `INSERT ... SELECT ... WHERE s.user_id = ? ON CONFLICT DO NOTHING`：归属条件
        写在 SELECT 里，因此**归属不通过时一行都插不进去**，不需要「先查一次再写」
        这种两次往返、留下窗口的写法。随后再查一次既拿模型对象又当归属校验的第二道闸。
        """
        insert_from_owned = sqlite_insert(Document).from_select(
            ["solution_id", "kind", "created_at", "updated_at"],
            select(
                Solution.id,
                literal(kind),
                literal(now),
                literal(now),
            ).where(Solution.id == solution_id, Solution.user_id == owner_id),
        )
        self.session.execute(
            insert_from_owned.on_conflict_do_nothing(
                index_elements=["solution_id", "kind"]
            )
        )
        return (
            self.session.execute(
                select(Document).where(
                    Document.solution_id == solution_id, Document.kind == kind
                )
            )
            .scalars()
            .first()
        )

    def _update_current_version(
        self,
        owner_id: int,
        solution_id: int,
        kind: str,
        *,
        content: str,
        content_sha: str,
        pending_items: str | None,
        last_run_summary: str | None,
        replace_generated_meta: bool,
        source: str,
    ) -> bool:
        """就地改当前版本的实现，两种口径共用一个入口。

        `replace_generated_meta=True`：正文 + 清单 + 回执同批替换（生成改写的就地落稿）。
        `replace_generated_meta=False`：只改正文（用户手改）。

        当前版本不存在时（`current_version_id IS NULL`）顺势建 `version_no=1`——这样「先手改、
        后生成」与「先生成、后手改」两条入口都能自洽，不必让调用方先判断有没有版本。
        """
        now = utc_now_text()
        document = self._ensure_document(owner_id, solution_id, kind, now)
        if document is None:
            return False

        current = document.current_version_id
        if current is None:
            version = DocumentVersion(
                document_id=document.id,
                version_no=1,
                content=content,
                content_sha=content_sha,
                pending_items=pending_items if pending_items is not None else "[]",
                last_run_summary=last_run_summary,
                source=source,
                note="",
                meta="{}",
                created_at=now,
                updated_at=now,
            )
            self.session.add(version)
            self.session.flush()
            document.current_version_id = version.id
            document.updated_at = now
            self.session.flush()
            return True

        version = self.session.get(DocumentVersion, current)
        if version is None:
            # 指针指向一条不存在的版本（理论上不该发生）：按「不可见」处理并让事务回滚。
            return False
        version.content = content
        version.content_sha = content_sha
        if replace_generated_meta:
            version.pending_items = pending_items or "[]"
            version.last_run_summary = last_run_summary
        version.updated_at = now
        document.updated_at = now
        self.session.flush()
        return True
