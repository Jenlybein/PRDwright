"""生成任务（generation_jobs）的数据仓储。

**数据隔离落在这一层，而不是路由层。** 与 `solution_repo` 的口径一致：除系统级维护
方法 `reap_running` 与管理员查询外，每个方法的第一个参数都是 `owner_id` 且**不接受
`None`**——没有「不过滤归属」的逃生口。所有查询、更新都把它写进 SQL 的 WHERE 条件，
越权访问在数据库层面就查不到这一行。

`draft_content` 走**合并写**：`job_service` 在内存里维护权威正文，按「≤300ms 或增量
>4KB」的节奏调用 `persist_progress` 落一份最新快照。因此本层不实现
`add_completed_section` 那种「读-改-写单段」的方法——那种写法要在单连接内 read-modify-write
才不会并发丢段，而合并写由调用方一次传入完整的 `completed_sections`，天然避免该问题。

## 形态（v6 / T16.8）

绑定 `Session` 的实例方法：`GenerationJobRepository(session).get(...)`。
**不 commit、不 rollback、不 close**——事务边界归调用方的 `session_scope()`。
返回 `GenerationJob` 模型对象（ADR-078）；管理面 JOIN 用户名的查询返回只读投影
`AdminJob`（模型 + owner_name），不返回 dict。条件更新（supersede / cancel / reap）
一律 `update(...).where(...)` 并返回 `rowcount`——调用方靠它判断「有没有真的改到」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlmodel import Session

from backend.core.clock import utc_now_text
from backend.core.models import GenerationJob, User

# 任务状态：running / completed / failed（本层不做枚举约束，取值由服务层保证）
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


def _dump_sections(sections: list[dict[str, Any]]) -> str:
    """把章节列表序列化成 JSON 文本（中文不转义，便于人工排查）。"""
    return json.dumps(sections or [], ensure_ascii=False)


def _dump_pending_items(items: list[dict[str, Any]] | None) -> str:
    """把待确认清单序列化成 JSON 文本（中文不转义）。`None` 视为空清单。"""
    return json.dumps(items or [], ensure_ascii=False)


# 供应商快照的四列，顺序与写入列一一对应。
_PROVIDER_SNAPSHOT_KEYS: tuple[str, ...] = (
    "provider_protocol",
    "provider_config_id",
    "provider_name",
    "provider_model",
)


def _provider_columns(provider: dict[str, str] | None) -> dict[str, str]:
    """把供应商快照字典摊成列字典，缺的补空串。

    永不返回 `None`：这四列是 `NOT NULL DEFAULT ''`，写 `NULL` 会被 SQLite 拒绝。
    调用方少传一个键（例如只有 protocol 没有 name）时补空串而不是报错——
    账单宁可少一列可读信息，也不该让任务收尾失败。
    """
    data = provider or {}
    return {key: str(data.get(key) or "") for key in _PROVIDER_SNAPSHOT_KEYS}


@dataclass(frozen=True, slots=True)
class AdminJob:
    """管理面投影：任务模型 + JOIN 出来的用户名。

    `owner_name` 来自 `users.username`；`as_dict()` 把模型全列与用户名合并成
    管理员视图需要的扁平 dict（`_job_view` 消费的就是它）。
    """

    job: GenerationJob
    owner_name: str

    def as_dict(self) -> dict[str, Any]:
        return {**self.job.model_dump(), "owner_name": self.owner_name}


class GenerationJobRepository:
    """`generation_jobs` 表的读写（绑定一个 `Session`）。所有用户面方法强制带 `owner_id`。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # 写：创建 / 进度 / 收尾
    # ------------------------------------------------------------------

    def create(
        self,
        owner_id: int,
        kind: str,
        solution_id: int | None,
        input_fingerprint: str | None = None,
        doc_kind: str = "",
        chat_stage: str = "",
    ) -> GenerationJob:
        """新建一条 running 任务，返回**模型对象**（已 `flush`，`id` 可用）。

        `doc_kind` 与 `kind` 分开：`kind` 是「做了什么」，`doc_kind` 是「写到哪」。
        `optimize` / `clarify` 这类动作凭 `kind` 定不了产物，只有请求体知道；
        创建时就把结论落列，刷新 / 进程重启后仍能回答「这个任务改的是哪一份」。

        `chat_stage` 同理：`kind='chat'` 定不了「哪条线程」，而并发判重与重连都要它。
        """
        now = utc_now_text()
        job = GenerationJob(
            owner_id=owner_id,
            solution_id=solution_id,
            kind=kind,
            doc_kind=doc_kind,
            chat_stage=chat_stage,
            status=STATUS_RUNNING,
            phase="",
            draft_content="",
            completed_sections="[]",
            pending_items="[]",
            input_fingerprint=input_fingerprint,
            usage=None,
            max_provider_switches=2,
            error_code="",
            error_message="",
            created_at=now,
            updated_at=now,
        )
        self.session.add(job)
        self.session.flush()  # 拿自增 id（替代旧的 lastrowid）
        return job

    def persist_progress(
        self,
        owner_id: int,
        job_id: int,
        *,
        draft_content: str,
        completed_sections: list[dict[str, Any]],
        phase: str,
        pending_items: list[dict[str, Any]] | None = None,
    ) -> None:
        """合并写一次进度（正文 / 章节 / 阶段 / 待确认清单）。WHERE 含 owner_id，改不到别人的任务。

        高频写（≤300ms / 增量 4KB）：**每次调用仍应是一个短事务**（调用方每次一个
        `session_scope()`），不要与其它写合并——长事务会持锁，把生成链路卡住。
        """
        self.session.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job_id, GenerationJob.owner_id == owner_id)
            .values(
                draft_content=draft_content,
                completed_sections=_dump_sections(completed_sections),
                phase=phase,
                pending_items=_dump_pending_items(pending_items),
                updated_at=utc_now_text(),
            )
        )

    def finish(
        self,
        owner_id: int,
        job_id: int,
        *,
        draft_content: str,
        completed_sections: list[dict[str, Any]],
        usage: str | None = None,
        pending_items: list[dict[str, Any]] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        provider: dict[str, str] | None = None,
    ) -> None:
        """标记任务完成，落最终正文与章节。清空错误字段。

        `prompt_tokens` / `completion_tokens` 与 `provider` 里的四列都是 `usage`
        那份 JSON 的**派生副本**：管理员后台统计消耗时用 SQL 直接求和，不必把每行
        JSON 取回解析（见迁移 v14 / v19）。

        `provider` 收的是一个字典，而不是四个位置参数：它们总是同时有值、同时为空，
        拆成四个参数只会让调用点写四行 `''` 兜底。
        """
        self.session.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job_id, GenerationJob.owner_id == owner_id)
            .values(
                status=STATUS_COMPLETED,
                draft_content=draft_content,
                completed_sections=_dump_sections(completed_sections),
                usage=usage,
                error_code="",
                error_message="",
                pending_items=_dump_pending_items(pending_items),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                **_provider_columns(provider),
                updated_at=utc_now_text(),
            )
        )

    def fail(
        self,
        owner_id: int,
        job_id: int,
        *,
        error_code: str,
        error_message: str,
        draft_content: str,
        completed_sections: list[dict[str, Any]],
        usage: str | None = None,
        pending_items: list[dict[str, Any]] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        provider: dict[str, str] | None = None,
    ) -> None:
        """标记任务失败，保留已产出的半截正文、章节与已发生的用量。

        `usage` 在失败路径同样要落：失败的生成**照样消耗了 token**，
        只记成功那次等于把最贵的失败藏起来。两个 token 列与供应商四列同理一并写：
        成本统计若跳过失败任务，「这次为什么这么贵」永远查不出来。
        """
        self.session.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job_id, GenerationJob.owner_id == owner_id)
            .values(
                status=STATUS_FAILED,
                error_code=error_code,
                error_message=error_message,
                draft_content=draft_content,
                completed_sections=_dump_sections(completed_sections),
                usage=usage,
                pending_items=_dump_pending_items(pending_items),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                **_provider_columns(provider),
                updated_at=utc_now_text(),
            )
        )

    # ------------------------------------------------------------------
    # 取消 / 顶替（条件更新，返回 rowcount）
    # ------------------------------------------------------------------

    def supersede_active(self, owner_id: int, *, solution_id: int | None, kind: str) -> int:
        """把同 owner + solution + kind 下旧的 running 置为 failed，返回被取代的条数。

        「同一 owner + solution + kind 至多一个 running」是一条业务不变量，在新建任务前
        先把旧的中止掉，避免两个任务同时往同一处写正文。
        `solution_id` 可能为 `None`，SQL 需用 `IS NULL` 而不是 `= NULL`。

        `error_code` 用 `SUPERSEDED` 而不是笼统的 `INTERNAL_ERROR`：这次失败有明确原因
        （被别人接手了，不是出错），运维看这一行时不该去翻日志找异常。
        """
        conditions = [
            GenerationJob.owner_id == owner_id,
            GenerationJob.kind == kind,
            GenerationJob.status == STATUS_RUNNING,
        ]
        conditions.append(
            GenerationJob.solution_id.is_(None)
            if solution_id is None
            else GenerationJob.solution_id == solution_id
        )
        result = self.session.execute(
            update(GenerationJob)
            .where(*conditions)
            .values(
                status=STATUS_FAILED,
                error_code="SUPERSEDED",
                error_message="被新任务取代",
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    def cancel_one(self, owner_id: int, job_id: int, *, message: str) -> bool:
        """把某一条 running 任务置为 failed + CANCELLED，返回是否确实改到了。

        与 `cancel_running`（按方案批量）分开：用户按「停止生成」针对的是**这一次**
        任务，而同一方案下别的线程可能也在跑，批量取消会连带掐掉它们。

        任务已经自然结束（`rowcount = 0`）时返回 False：调用方据此回答
        「没取消到东西」，而不是报错——「你点的时候它刚好说完了」不是失败。
        """
        result = self.session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.owner_id == owner_id,
                GenerationJob.id == job_id,
                GenerationJob.status == STATUS_RUNNING,
            )
            .values(
                status=STATUS_FAILED,
                error_code="CANCELLED",
                error_message=message,
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0) == 1

    def cancel_running(self, owner_id: int, *, solution_id: int) -> int:
        """把该 owner + 方案下所有 running 任务置为 failed，返回受影响条数。

        用在「方案被删除」：方案没了，挂在它上面的生成任务再跑完也没地方落正文，
        继续跑只会白烧 token。

        ⚠️ **刻意复用 `failed` 而不是新造一个 `cancelled` 状态**：任务状态是前后端共享
        的契约（前端所有 status 分支都按 running / completed / failed 三态判），
        多一个取值意味着每一处分支都要多一条分支，而漏掉任何一处都会让界面停在
        「生成中」。语义上被取消确实「没有成功」，复用 `failed` 是代价最小的选择；
        具体原因放在 `error_code='CANCELLED'` / `error_message` 里。

        `solution_id` 用 `= ?` 而不是 `IS NULL`：本方法只服务于「删掉一份具体方案」，
        没有方案的任务（快捷入口逃生口）不会被这个动作波及。
        """
        result = self.session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.owner_id == owner_id,
                GenerationJob.solution_id == solution_id,
                GenerationJob.status == STATUS_RUNNING,
            )
            .values(
                status=STATUS_FAILED,
                error_code="CANCELLED",
                error_message="方案已删除，任务已取消",
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    def cancel_running_for_owner(self, owner_id: int, *, message: str) -> int:
        """把该 owner 名下**所有** running 任务置为 failed + CANCELLED，返回条数。

        用在「删除账号」：账号一旦消失，任务再跑完也没有地方落正文，继续跑只是白烧
        token——而且收尾那一刻还会去写一个已经不存在的归属。

        与 `cancel_running`（按方案）的区别只有过滤维度：删账号要覆盖他名下**所有**
        方案，包括还没有方案的逃生口任务（`solution_id IS NULL`），所以这里只按
        `owner_id` 过滤，不加 `solution_id` 条件。
        """
        result = self.session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.owner_id == owner_id,
                GenerationJob.status == STATUS_RUNNING,
            )
            .values(
                status=STATUS_FAILED,
                error_code="CANCELLED",
                error_message=message,
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    def reap_running(self) -> int:
        """系统级维护：把所有残留的 running 任务置为 failed，返回条数。

        **这是本仓库唯一没有 `owner_id` 的用户面外方法**，属于启动运维（进程重启后，
        内存里的运行时已丢失，DB 里不可能还有真正在跑的任务），**不是用户面接口**。
        调用点只有 `job_service.reap_orphans()`，即在应用 `lifespan` 启动时回收上次
        崩溃 / 重启留下的悬挂任务。
        """
        result = self.session.execute(
            update(GenerationJob)
            .where(GenerationJob.status == STATUS_RUNNING)
            .values(
                status=STATUS_FAILED,
                error_code="INTERNAL_ERROR",
                error_message="服务重启，任务已中断",
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, owner_id: int, job_id: int) -> GenerationJob | None:
        """读取单条任务；不存在或不属于该用户时返回 None。"""
        return (
            self.session.execute(
                select(GenerationJob).where(
                    GenerationJob.id == job_id, GenerationJob.owner_id == owner_id
                )
            )
            .scalars()
            .first()
        )

    def find_active(
        self,
        owner_id: int,
        *,
        solution_id: int | None = None,
        kind: str | None = None,
        chat_stage: str | None = None,
    ) -> GenerationJob | None:
        """查该 owner 下最近的 running 任务（`updated_at DESC`）。

        `solution_id` / `kind` / `chat_stage` 为 `None` 时不参与过滤。
        """
        conditions = [
            GenerationJob.owner_id == owner_id,
            GenerationJob.status == STATUS_RUNNING,
        ]
        if solution_id is not None:
            conditions.append(GenerationJob.solution_id == solution_id)
        if kind is not None:
            conditions.append(GenerationJob.kind == kind)
        if chat_stage is not None:
            conditions.append(GenerationJob.chat_stage == chat_stage)
        return (
            self.session.execute(
                select(GenerationJob)
                .where(*conditions)
                .order_by(GenerationJob.updated_at.desc(), GenerationJob.id.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

    def list_by_owner(
        self,
        owner_id: int,
        *,
        status: str | None = None,
        solution_id: int | None = None,
        kind: str | None = None,
    ) -> list[GenerationJob]:
        """列出该 owner 的任务，最近创建在前；可选按 status / solution_id / kind 过滤。"""
        conditions = [GenerationJob.owner_id == owner_id]
        if status is not None:
            conditions.append(GenerationJob.status == status)
        if solution_id is not None:
            conditions.append(GenerationJob.solution_id == solution_id)
        if kind is not None:
            conditions.append(GenerationJob.kind == kind)
        return list(
            self.session.execute(
                select(GenerationJob)
                .where(*conditions)
                .order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc())
            )
            .scalars()
            .all()
        )

    # ------------------------------------------------------------------
    # 管理面查询（跨 owner，安全性由路由层 require_admin 保证）
    # ------------------------------------------------------------------

    def admin_list(
        self,
        *,
        status: str | None = None,
        username: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AdminJob]:
        """列出任务（管理员视角，JOIN 出用户名），返回 `AdminJob` 投影。

        排序按状态分开：
        - `running` 用 `created_at ASC`——卡死任务的特征就是「跑得最久」，
          把它顶到最前面才看得见；
        - 其余用 `updated_at DESC`——最近有动静的在前。

        `username` 是**模糊匹配**（`LIKE`）：管理员通常只记得半个用户名，
        要求精确输入等于让他先去用户页复制一遍。
        """
        conditions = []
        if status:
            conditions.append(GenerationJob.status == status)
        if kind:
            conditions.append(GenerationJob.kind == kind)
        if username:
            conditions.append(User.username.like(f"%{username}%"))

        order = (
            (GenerationJob.created_at.asc(), GenerationJob.id.asc())
            if status == STATUS_RUNNING
            else (GenerationJob.updated_at.desc(), GenerationJob.id.desc())
        )
        rows = self.session.execute(
            select(GenerationJob, User.username)
            .join(User, User.id == GenerationJob.owner_id)
            .where(*conditions)
            .order_by(*order)
            .limit(limit)
            .offset(offset)
        ).all()
        return [AdminJob(job=row[0], owner_name=row[1] or "") for row in rows]

    def admin_count(
        self,
        *,
        status: str | None = None,
        username: str | None = None,
        kind: str | None = None,
    ) -> int:
        """与 `admin_list` 同口径的命中总数（分页 / 「还有多少」用）。"""
        conditions = []
        if status:
            conditions.append(GenerationJob.status == status)
        if kind:
            conditions.append(GenerationJob.kind == kind)
        if username:
            conditions.append(User.username.like(f"%{username}%"))
        total = self.session.execute(
            select(func.count())
            .select_from(GenerationJob)
            .join(User, User.id == GenerationJob.owner_id)
            .where(*conditions)
        ).scalar_one()
        return int(total or 0)

    def get_for_admin(self, job_id: int) -> AdminJob | None:
        """按 id 取**任意用户**的任务（管理面取消前定位 owner），返回 `AdminJob` 投影。

        ⚠️ 跨 owner，非用户面方法。调用方必须先过 `require_admin`。
        用户名一并 JOIN 出来，用于审计里写「取消了谁的任务」——只记一个 id
        会让事后追责时要再查一次库。
        """
        row = self.session.execute(
            select(GenerationJob, User.username)
            .join(User, User.id == GenerationJob.owner_id)
            .where(GenerationJob.id == job_id)
        ).first()
        if row is None:
            return None
        return AdminJob(job=row[0], owner_name=row[1] or "")
