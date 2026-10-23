"""方案（solutions）的数据仓储。

**数据隔离落在这一层，而不是路由层。**

本文件里每个方法的第一个参数都是 `owner_id`，并且**不接受 `None`**——没有「不过滤归属」
的逃生口。所有查询、更新、删除都把它写进 SQL 的 WHERE 条件，因此越权访问在数据库层面
就查不到这一行；路由层只需要把「查不到」翻译成 404，不必在每个接口里记得写一次权限判断。
安全边界不该寄托在「每个写接口的人都记得」。

一条状态拆成列，而不是一坨 JSON
------------------------------
`solutions.data` 曾经是整份 `SessionDataV2` 的 JSON 文本，本层刻意「从不解析它的内容」。
那条约定的前提是**只有一个写者**：快照永远由同一台前端整份覆盖，原子且简单。

会话被多处访问（两台设备、切方案回切）之后，前提不成立了：整份覆盖意味着后写的
把先写的全部盖掉。于是方案状态拆成三层，各自按读写模式落地（见 `migrations` v9）：

- **相位与草稿** → 本表的标量列。它们仍是「值」，一次 UPSERT 原子生效，只是不再
  和记录混在同一格里；
- **聊天** → `chat_turns`（只追加，服务端单写者）；
- **会话 id** → `chat_sessions`（按方案 + 线程唯一）。

`data` 列保留但**不再写也不再读**：它是旧快照的最后一份副本，留着当历史备份；
新行写空串（该列 `NOT NULL`），运行期没有任何代码读它。

## 形态（v6 / T16.7）

绑定 `Session` 的实例方法：`SolutionRepository(session).get(...)`。
**不 commit、不 rollback、不 close**——事务边界归调用方的 `session_scope()`。
返回 `Solution` 模型对象（ADR-078），不再是 `dict`：`row.stage` 写错会被类型检查抓住，
`row["stage"]` 要等运行时。需要自增 id 时用 `flush()` + `solution.id`，不再用 `lastrowid`。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select, update
from sqlmodel import Session

from backend.core.clock import utc_now_text
from backend.core.models import Solution

# 方案状态列（与 `migrations._SOLUTION_STATE_COLUMNS` 一一对应）。
# 列名放在这里而不是散在各方法里：INSERT / UPDATE / 读取三处若各写一遍，加一列时
# 一定会漏掉其中一处，而症状是「保存了但刷新就丢」这种最难查的表现。
STATE_COLUMNS: tuple[str, ...] = (
    "view_state",
    "entry_mode",
    "chat_tab",
    "summary_json",
    "quick_product_name",
    "quick_requirements",
    "draft_prd_text",
    "paste_prd_text",
    "paste_api_docs_text",
    "active_job_id",
    "active_job_kind",
    "active_job_doc_kind",
)

# 状态列的默认值：字符串一律空串、`active_job_id` 为 None。
# 单独抽出来，是为了让 create / update 用同一套「缺失即默认」的语义，
# 不会出现「创建时缺某列写成 NULL、更新时写成空串」这种两套表示法。
_STATE_DEFAULTS: dict[str, Any] = {
    "view_state": "",
    "entry_mode": "",
    "chat_tab": "",
    "summary_json": "",
    "quick_product_name": "",
    "quick_requirements": "",
    "draft_prd_text": "",
    "paste_prd_text": "",
    "paste_api_docs_text": "",
    "active_job_id": None,
    "active_job_kind": "",
    "active_job_doc_kind": "",
}


def _state_values(state: dict[str, Any] | None) -> dict[str, Any]:
    """把外部传进来的状态裁成仓储认识的列值；未提供的列取默认值。"""
    values = dict(_STATE_DEFAULTS)
    values.update({k: v for k, v in (state or {}).items() if k in STATE_COLUMNS})
    return values


class SolutionRepository:
    """`solutions` 表的读写（绑定一个 `Session`）。所有方法强制带 `owner_id`。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def create(
        self,
        owner_id: int,
        title: str,
        mode: str,
        stage: str,
        state: dict[str, Any] | None = None,
    ) -> Solution:
        """新建方案并返回**模型对象**（已 `flush`，`id` 可用）。

        方案是**有内容了才建**的，所以创建这一刻内容必然非空：两根时间轴都从此刻起步。

        `data` 写空串：这一列已退役。`NOT NULL` 是历史约束，给空串比给一份
        「看起来像快照、其实没人读」的 JSON 更诚实。
        """
        now = utc_now_text()
        solution = Solution(
            user_id=owner_id,
            title=title,
            mode=mode,
            stage=stage,
            data="",
            created_at=now,
            updated_at=now,
            content_updated_at=now,
            **_state_values(state),
        )
        self.session.add(solution)
        self.session.flush()  # 拿自增 id（替代旧的 lastrowid）
        return solution

    def update(
        self,
        owner_id: int,
        solution_id: int,
        title: str,
        mode: str,
        stage: str,
        state: dict[str, Any] | None,
        content_changed: bool,
    ) -> bool:
        """整份覆盖状态列，成功返回 True。

        `user_id` 在 WHERE 条件里，因此**改不到别人的方案**；匹配不到时 `rowcount` 为 0。
        这里刻意不区分「方案不存在」与「方案不是你的」——区分了反而会泄露资源是否存在。

        `content_changed` 决定要不要推进 `content_updated_at`；为假时**不动该列**
        （等价于原来的 `CASE WHEN ... ELSE content_updated_at END`），而不是写成此刻——
        切阶段、翻看这类保存天天发生，把它们记成内容变更就等于这根轴不存在。

        `updated_at` 照旧每次都写：它回答的是「这份记录最近一次被写是什么时候」，
        这件事确实刚刚发生过。

        ⚠️ **不写 `data`**：那一列已退役，保持旧值当历史备份。新行由 `create` 写空串。
        """
        now = utc_now_text()
        values = _state_values(state)
        values.update({"title": title, "mode": mode, "stage": stage, "updated_at": now})
        # 只有内容真的变了才推进 content_updated_at；否则不放进 values，
        # 该列在 UPDATE 里缺席 = 保持原值，与 CASE WHEN 的语义一致。
        if content_changed:
            values["content_updated_at"] = now

        result = self.session.execute(
            update(Solution)
            .where(Solution.id == solution_id, Solution.user_id == owner_id)
            .values(**values)
        )
        return int(result.rowcount or 0) == 1

    def delete(self, owner_id: int, solution_id: int) -> bool:
        """删除方案，成功返回 True。归属不匹配时同 `update`，返回 False。

        `chat_turns` / `chat_sessions` 靠库里的 `ON DELETE CASCADE` 一起走：
        级联必须在库里，不能靠调用方记得。
        """
        result = self.session.execute(
            delete(Solution).where(Solution.id == solution_id, Solution.user_id == owner_id)
        )
        return int(result.rowcount or 0) == 1

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, owner_id: int, solution_id: int) -> Solution | None:
        """读取单条方案（含全部状态列）；不存在或不属于该用户时返回 None。"""
        return (
            self.session.execute(
                select(Solution).where(
                    Solution.id == solution_id, Solution.user_id == owner_id
                )
            )
            .scalars()
            .first()
        )

    def list_by_owner(self, owner_id: int) -> list[Solution]:
        """列出该用户的全部方案，按**内容时间**倒序。

        排序用 `content_updated_at` 而不是 `updated_at`：后者只有切阶段也会被刷新，
        于是「刚点开看过的那份」会莫名排到最前。列表的顺序应该由作品本身的变化决定。
        """
        return list(
            self.session.execute(
                select(Solution)
                .where(Solution.user_id == owner_id)
                .order_by(Solution.content_updated_at.desc(), Solution.id.desc())
            )
            .scalars()
            .all()
        )

    def exists(self, owner_id: int, solution_id: int) -> bool:
        """方案是否存在且属于该用户。

        **只做存在性判断，不取状态列**：文档接口需要在「返回空列表」之前区分
        「这份方案不属于你」与「它确实还没有文档」，而前者只需一次存在性查询。
        """
        row = self.session.execute(
            select(Solution.id).where(
                Solution.id == solution_id, Solution.user_id == owner_id
            )
        ).first()
        return row is not None
