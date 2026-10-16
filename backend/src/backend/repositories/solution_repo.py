"""方案（solutions）的数据仓储。

**数据隔离落在这一层，而不是路由层。**

本文件里每个方法的第一个参数都是 `owner_id`，并且**不接受 `None`**——没有「不过滤归属」
的逃生口。所有查询、更新、删除都把它写进 SQL 的 WHERE 条件，因此越权访问在数据库层面
就查不到这一行；路由层只需要把「查不到」翻译成 404，不必在每个接口里记得写一次权限判断。
安全边界不该寄托在「每个写接口的人都记得」。

另一个约定：`data` 列存的是 `SessionDataV2` 的 JSON 文本，本层**从不解析它的内容**。
保持「不透明快照」的好处是前端调整快照字段时后端不需要跟着改。
"""

from __future__ import annotations

from typing import Any

from backend.core.db import connect, utc_now_text


class SolutionRepository:
    """`solutions` 表的读写。所有方法强制带 `owner_id`。"""

    @staticmethod
    def create(owner_id: int, title: str, mode: str, stage: str, data: str) -> int:
        """新建方案，返回其 id。

        方案是**有内容了才建**的，所以创建这一刻内容必然非空：两根时间轴都从此刻起步。
        """
        now = utc_now_text()
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO solutions "
                "(user_id, title, mode, stage, data, created_at, updated_at, content_updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (owner_id, title, mode, stage, data, now, now, now),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    @staticmethod
    def update(
        owner_id: int,
        solution_id: int,
        title: str,
        mode: str,
        stage: str,
        data: str,
        content_changed: bool,
    ) -> bool:
        """整份覆盖更新，成功返回 True。

        `user_id` 在 WHERE 条件里，因此**改不到别人的方案**；匹配不到时 `rowcount` 为 0。
        这里刻意不区分「方案不存在」与「方案不是你的」——区分了反而会泄露资源是否存在。

        `content_changed` 决定要不要推进 `content_updated_at`；为假时**保持原值**
        （`CASE WHEN ... ELSE content_updated_at END`），而不是写成 `NULL` 或此刻——
        切阶段、翻看这类保存天天发生，把它们记成内容变更就等于这根轴不存在。

        `updated_at` 照旧每次都写：它回答的是「这份记录最近一次被写是什么时候」，
        这件事确实刚刚发生过。
        """
        now = utc_now_text()
        with connect() as conn:
            cur = conn.execute(
                "UPDATE solutions SET title = ?, mode = ?, stage = ?, data = ?, "
                "updated_at = ?, "
                "content_updated_at = CASE WHEN ? THEN ? ELSE content_updated_at END "
                "WHERE id = ? AND user_id = ?",
                (
                    title,
                    mode,
                    stage,
                    data,
                    now,
                    1 if content_changed else 0,
                    now,
                    solution_id,
                    owner_id,
                ),
            )
            conn.commit()
            return cur.rowcount == 1

    @staticmethod
    def get(owner_id: int, solution_id: int) -> dict[str, Any] | None:
        """读取单条方案（含 `data` 全量）；不存在或不属于该用户时返回 None。"""
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM solutions WHERE id = ? AND user_id = ?",
                (solution_id, owner_id),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def list_by_owner(owner_id: int) -> list[dict[str, Any]]:
        """列出该用户的全部方案摘要，按**内容时间**倒序。

        **只 SELECT 摘要列，不读 `data`**：那份 JSON 含三份文档正文，几十 KB 起步，
        列二十条时代价很明显。列表页要的只是标题与进度，没有理由把正文一起取出来。

        排序用 `content_updated_at` 而不是 `updated_at`：后者只有切阶段也会被刷新，
        于是「刚点开看过的那份」会莫名排到最前。列表的顺序应该由作品本身的变化决定。
        """
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, title, mode, stage, created_at, updated_at, content_updated_at "
                "FROM solutions WHERE user_id = ? "
                "ORDER BY content_updated_at DESC, id DESC",
                (owner_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def delete(owner_id: int, solution_id: int) -> bool:
        """删除方案，成功返回 True。归属不匹配时同 `update`，返回 False。"""
        with connect() as conn:
            cur = conn.execute(
                "DELETE FROM solutions WHERE id = ? AND user_id = ?",
                (solution_id, owner_id),
            )
            conn.commit()
            return cur.rowcount == 1

    @staticmethod
    def exists(owner_id: int, solution_id: int) -> bool:
        """方案是否存在且属于该用户。

        **只做存在性判断，不取 `data`**：文档接口需要在「返回空列表」之前区分
        「这份方案不属于你」与「它确实还没有文档」，而前者只需一次存在性查询，
        没有理由把几十 KB 的快照取回来。
        """
        with connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM solutions WHERE id = ? AND user_id = ?",
                (solution_id, owner_id),
            ).fetchone()
            return row is not None
