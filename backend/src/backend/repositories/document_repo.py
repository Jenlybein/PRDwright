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
  `connect()` 不自动提交，事务边界由本模块显式给出。
"""

from __future__ import annotations

from typing import Any

from backend.core.db import connect, utc_now_text

# 「文档行 + 当前版本行」的合并取数：对外读文档的口径全走它。
# 用字段名与 `_to_item` 对齐（content / pending_items / last_run_summary / updated_at），
# 另附归属与版本标识字段供服务层使用。
_MERGED_COLUMNS = (
    "SELECT d.id AS document_id, d.solution_id, d.kind, d.created_at, d.updated_at, "
    "       d.current_version_id, "
    "       v.id AS version_id, v.version_no, v.content, v.content_sha, "
    "       v.pending_items, v.last_run_summary, v.source, v.metadata "
    "FROM documents d "
    "LEFT JOIN document_versions v ON v.id = d.current_version_id "
    "JOIN solutions s ON s.id = d.solution_id "
)


class DocumentRepository:
    """`documents` / `document_versions` 的读写。所有方法强制带 `owner_id`。"""

    # ------------------------------------------------------------------
    # 读：当前文档
    # ------------------------------------------------------------------

    @staticmethod
    def list_by_solution(owner_id: int, solution_id: int) -> list[dict[str, Any]]:
        """列出某方案下的全部文档（至多三份）连同各自的当前版本，按写入先后排序。"""
        with connect() as conn:
            rows = conn.execute(
                _MERGED_COLUMNS + "WHERE s.user_id = ? AND d.solution_id = ? ORDER BY d.id",
                (owner_id, solution_id),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def get(owner_id: int, solution_id: int, kind: str) -> dict[str, Any] | None:
        """读取一份文档（含当前版本）；不存在、或方案不属于该用户时返回 None。"""
        with connect() as conn:
            row = conn.execute(
                _MERGED_COLUMNS + "WHERE s.user_id = ? AND d.solution_id = ? AND d.kind = ?",
                (owner_id, solution_id, kind),
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # 读：版本链
    # ------------------------------------------------------------------

    @staticmethod
    def list_versions(owner_id: int, solution_id: int, kind: str) -> list[dict[str, Any]]:
        """列出某文档的全部版本摘要（**不带正文**），按版本号从新到旧。

        返回值含 `is_current`（该版本是否当前版本）。正文只在取单个版本详情时读——
        版本列表可能很长，把正文都拖出来是纯浪费。
        """
        with connect() as conn:
            rows = conn.execute(
                "SELECT v.id AS version_id, v.version_no, v.source, v.note, v.content_sha, "
                "       LENGTH(v.content) AS content_length, v.created_at, v.updated_at, "
                "       (v.id = d.current_version_id) AS is_current "
                "FROM document_versions v "
                "JOIN documents d ON d.id = v.document_id "
                "JOIN solutions s ON s.id = d.solution_id "
                "WHERE s.user_id = ? AND d.solution_id = ? AND d.kind = ? "
                "ORDER BY v.version_no DESC",
                (owner_id, solution_id, kind),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def get_version(
        owner_id: int, solution_id: int, kind: str, version_id: int
    ) -> dict[str, Any] | None:
        """读取一条版本的完整内容（含正文 / 清单 / 回执）；不属于该用户时返回 None。"""
        with connect() as conn:
            row = conn.execute(
                "SELECT v.id AS version_id, v.version_no, v.content, v.content_sha, "
                "       v.pending_items, v.last_run_summary, v.source, v.note, v.metadata, "
                "       v.created_at, v.updated_at, "
                "       (v.id = d.current_version_id) AS is_current "
                "FROM document_versions v "
                "JOIN documents d ON d.id = v.document_id "
                "JOIN solutions s ON s.id = d.solution_id "
                "WHERE s.user_id = ? AND d.solution_id = ? AND d.kind = ? AND v.id = ?",
                (owner_id, solution_id, kind, version_id),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def delete_version(owner_id: int, solution_id: int, kind: str, version_id: int) -> bool:
        """删除一条**历史**版本，删掉了返回 True。

        ⚠️ **当前版本删不得**，而且这条限制写在 SQL 里（`v.id != d.current_version_id`）
        而不是「先查一次、再决定删不删」：两次查询之间若发生别的写入（例如另一个标签页
        刚好把它恢复成当前版本），先查后删就会放行——那时删掉的是文档正在用的正文。
        把判据和动作收在同一条语句里，这个窗口就不存在。

        归属仍经 `documents → solutions` 两级 JOIN 过滤，越权与不存在一样「删不掉」。

        删除**历史**版本不影响当前正文，因此这里刻意不去动 `documents.updated_at`——
        它表示「这份文档的当前内容什么时候变的」，删一条旧版并没有改变这件事。
        """
        with connect() as conn:
            cursor = conn.execute(
                "DELETE FROM document_versions WHERE id IN ("
                "  SELECT v.id FROM document_versions v "
                "  JOIN documents d ON d.id = v.document_id "
                "  JOIN solutions s ON s.id = d.solution_id "
                "  WHERE s.user_id = ? AND d.solution_id = ? AND d.kind = ? AND v.id = ? "
                "    AND v.id != COALESCE(d.current_version_id, -1)"
                ")",
                (owner_id, solution_id, kind, version_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # 写：追加版本 / 就地改当前版本
    # ------------------------------------------------------------------

    @staticmethod
    def append_version(
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
    ) -> int | None:
        """追加一条新版本并把它设为当前版本；返回新版本 id，方案不可见时返回 None。

        `invalidate` 列出「被本次生成取代、应当删除」的**产物种类**（下游产物从上游正文
        推导而来，上游重写后它们就失效了）。删除与追加**在同一事务里**完成：若分成两次
        提交，中间那一瞬就是「新正文已到、旧下游还在」，刷新页面会看到一份自相矛盾的方案。
        删下游文档会**级联删掉它们的版本**，无需另写删版本语句。

        四步顺序不能变（环形外键 `documents.current_version_id ↔ document_versions.document_id`
        决定的）：删下游 → **先空指针插入文档行** → 插版本行 → 再回填指针。反过来会撞上
        「指针指向还不存在的版本」。归属不通过时第二步一行都插不进去、第三步查不到行，
        于是整体回滚返回 None。
        """
        now = utc_now_text()
        with connect() as conn:
            for stale_kind in invalidate:
                conn.execute(
                    "DELETE FROM documents WHERE id IN ("
                    "  SELECT d.id FROM documents d "
                    "  JOIN solutions s ON s.id = d.solution_id "
                    "  WHERE s.user_id = ? AND d.solution_id = ? AND d.kind = ?"
                    ")",
                    (owner_id, solution_id, stale_kind),
                )
            document_id = _ensure_document(conn, owner_id, solution_id, kind, now)
            if document_id is None:
                conn.rollback()
                return None
            next_no = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 FROM document_versions "
                    "WHERE document_id = ?",
                    (document_id,),
                ).fetchone()[0]
            )
            cursor = conn.execute(
                "INSERT INTO document_versions "
                "(document_id, version_no, content, content_sha, pending_items, "
                " last_run_summary, source, note, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    next_no,
                    content,
                    content_sha,
                    pending_items,
                    last_run_summary,
                    source,
                    note,
                    metadata,
                    now,
                    now,
                ),
            )
            version_id = int(cursor.lastrowid or 0)
            conn.execute(
                "UPDATE documents SET current_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, now, document_id),
            )
            conn.commit()
            return version_id

    @staticmethod
    def update_current_version(
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
        return _update_current_version(
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

    @staticmethod
    def replace_current_version(
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
        return _update_current_version(
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


def _ensure_document(
    conn: Any, owner_id: int, solution_id: int, kind: str, now: str
) -> int | None:
    """确保 `documents` 行存在并返回其 id；方案不属于该用户时返回 None。

    `INSERT ... SELECT ... WHERE s.user_id = ?` 带上了归属条件，因此**归属不通过时一行
    都插不进去**，不需要先查一次再写；后续再查一次既拿 id 又当归属校验的第二道闸。
    """
    conn.execute(
        "INSERT INTO documents (solution_id, kind, created_at, updated_at) "
        "SELECT s.id, ?, ?, ? FROM solutions s WHERE s.id = ? AND s.user_id = ? "
        "ON CONFLICT(solution_id, kind) DO NOTHING",
        (kind, now, now, solution_id, owner_id),
    )
    row = conn.execute(
        "SELECT d.id AS document_id FROM documents d "
        "JOIN solutions s ON s.id = d.solution_id "
        "WHERE s.user_id = ? AND d.solution_id = ? AND d.kind = ?",
        (owner_id, solution_id, kind),
    ).fetchone()
    return int(row["document_id"]) if row else None


def _update_current_version(
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
    with connect() as conn:
        document_id = _ensure_document(conn, owner_id, solution_id, kind, now)
        if document_id is None:
            conn.rollback()
            return False

        row = conn.execute(
            "SELECT current_version_id FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        current = row["current_version_id"] if row else None

        if current is None:
            cursor = conn.execute(
                "INSERT INTO document_versions "
                "(document_id, version_no, content, content_sha, pending_items, "
                " last_run_summary, source, note, metadata, created_at, updated_at) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, '', '{}', ?, ?)",
                (
                    document_id,
                    content,
                    content_sha,
                    pending_items if pending_items is not None else "[]",
                    last_run_summary,
                    source,
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE documents SET current_version_id = ?, updated_at = ? WHERE id = ?",
                (int(cursor.lastrowid or 0), now, document_id),
            )
        elif replace_generated_meta:
            conn.execute(
                "UPDATE document_versions SET content = ?, content_sha = ?, "
                "pending_items = ?, last_run_summary = ?, updated_at = ? WHERE id = ?",
                (content, content_sha, pending_items or "[]", last_run_summary, now, int(current)),
            )
            conn.execute("UPDATE documents SET updated_at = ? WHERE id = ?", (now, document_id))
        else:
            conn.execute(
                "UPDATE document_versions SET content = ?, content_sha = ?, updated_at = ? "
                "WHERE id = ?",
                (content, content_sha, now, int(current)),
            )
            conn.execute("UPDATE documents SET updated_at = ? WHERE id = ?", (now, document_id))

        conn.commit()
        return True
