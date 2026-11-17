"""生成任务（generation_jobs）的数据仓储。

**数据隔离落在这一层，而不是路由层。** 与 `solution_repo` 的口径一致：除两个系统级
维护读 `claimable`（扫待领任务，#82）与 `scan_recoverable`（扫该接手的过期租约，#83）
以及管理员查询外，每个方法的第一个参数都是 `owner_id` 且**不接受 `None`**——没有
「不过滤归属」的逃生口。所有查询、更新都把它写进 SQL 的 WHERE 条件，越权访问在数据库
层面就查不到这一行。

`draft_content` 走**合并写**：`job_service` 在内存里维护权威正文，按「≤300ms 或增量
>4KB」的节奏调用 `persist_progress` 落一份最新快照。因此本层不实现
`add_completed_section` 那种「读-改-写单段」的方法——那种写法要在单连接内 read-modify-write
才不会并发丢段，而合并写由调用方一次传入完整的 `completed_sections`，天然避免该问题。

## 形态（v6 / T16.8）

绑定 `Session` 的实例方法：`GenerationJobRepository(session).get(...)`。
**不 commit、不 rollback、不 close**——事务边界归调用方的 `session_scope()`。
返回 `GenerationJob` 模型对象；管理面 JOIN 用户名的查询返回只读投影
`AdminJob`（模型 + owner_name），不返回 dict。条件更新（supersede / cancel / claim /
恢复接手）一律 `update(...).where(...)` 并返回 `rowcount`——调用方靠它判断「有没有真的
改到」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlmodel import Session

from backend.core.clock import utc_now_text
from backend.core.limits import ensure_text_within_limit
from backend.core.models import GenerationJob, User

# 任务状态：running / completed / failed（本层不做枚举约束，取值由服务层保证）
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


def _due_now(now_text: str):
    """**到期闸门**：这一行现在能不能被领走（票 #88）。

    `next_run_at` 是失败退避写下的「最早可再被领走的时刻」，空串表示「立即」。
    判据是 ISO 文本的字符串比较——字典序 = 时间序（`core/clock.py` 的口径），
    不需要把时间取回 Python 再判。

    ## 为什么只写成一处

    「先看是否到期、到期之后才按更新时间排队」（AC3）这条规则里，「是否到期」是**闸门**
    而不是排序键：闸门在 `WHERE` 里（未到期的行一行都改不到 / 选不出来），闸门之后的
    `ORDER BY` 才是排队顺序（`_queue_order`）。两处各写一遍必然有一天不一致，
    而那种不一致不会报错，只会表现成「某条任务被提前领走」或者「永远领不走」。

    `now_text` 为空串时判据退化成「只认空串 = 立即」：调用方总是会传判据时刻，
    这里不替它兜底成「现在」——判据与修改必须看到同一秒。
    """
    return or_(
        GenerationJob.next_run_at == "",
        GenerationJob.next_run_at <= now_text,
    )


def _queue_order() -> tuple:
    """**排队顺序**（AC3 的唯一一处定义）：到期之后按 `updated_at` 排，同一秒按 id。

    「回排队就更新 `updated_at`」是既有语义：回排队（`requeue_lease`）把这一列写成
    当前时刻，于是它排到同优先级队尾。加了 `next_run_at` 之后两套语义叠加，规则固定为
    ——**先过到期闸门（`_due_now`），到期之后才按这一列排队**。`created_at` 不再参与：
    建得早不等于该先跑，回排队过的任务应当让位给刚建的同优先级任务。
    """
    return (GenerationJob.updated_at, GenerationJob.id)


def _dump_sections(sections: list[dict[str, Any]]) -> str:
    """把章节列表序列化成 JSON 文本（中文不转义，便于人工排查）。"""
    return json.dumps(sections or [], ensure_ascii=False)


def _dump_pending_items(items: list[dict[str, Any]] | None) -> str:
    """把待确认清单序列化成 JSON 文本（中文不转义）。`None` 视为空清单。"""
    return json.dumps(items or [], ensure_ascii=False)


def _ensure_progress_texts(draft_content: str, sections_text: str) -> None:
    """草稿与章节清单的 60KB 闸门（v8）。

    这两列都是**渐进增长**的（分章生成时一章一章追加），所以超上限通常意味着
    「这份 PRD 比预期长」，而不是「某个字段填错了」。文案因此指向"精简或拆分"，
    而不是"请求非法"。

    ⚠️ 它会在 `persist_progress`（每 300ms 一次）里被调用，所以
    `ensure_text_within_limit` 的快速路径很重要——见 `core/limits.py` 的说明。
    """
    ensure_text_within_limit(draft_content, field="草稿正文")
    ensure_text_within_limit(sections_text, field="章节清单")


def _dump_immutable(request_params: Any, config_snapshot: Any, skill_refs: Any) -> tuple[str, str, str]:
    """把任务单的不可变数据序列化成三列 JSON 文本，并按既有 60KB 口径拦超限。

    这三列**只在建任务时写一次**，与 `_ensure_progress_texts` 分开：后者专管
    渐进增长的正文/章节清单，每 300ms 都要跑一次；这里只在 `create` 跑一次。
    上限仍是同一个 `ensure_text_within_limit`（不另立口径、不新增错误码）。
    """
    request_text = json.dumps(request_params or {}, ensure_ascii=False, default=str)
    config_text = json.dumps(config_snapshot or {}, ensure_ascii=False, default=str)
    skills_text = json.dumps(skill_refs or [], ensure_ascii=False, default=str)
    ensure_text_within_limit(request_text, field="原始请求参数")
    ensure_text_within_limit(config_text, field="配置快照")
    ensure_text_within_limit(skills_text, field="技能包身份")
    return request_text, config_text, skills_text


def _skill_manifest(skill_refs: Any) -> str:
    """取主技能包的可索引短身份；没有技能包时为空串（不猜占位值）。"""
    if not isinstance(skill_refs, list) or not skill_refs:
        return ""
    first = skill_refs[0]
    if not isinstance(first, dict):
        return ""
    parts = [str(first.get(key) or "") for key in ("name", "version", "fingerprint")]
    return "|".join(parts)[:64]


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
        *,
        request_params: Any = None,
        config_snapshot: Any = None,
        skill_refs: Any = None,
    ) -> GenerationJob:
        """新建一条 running 任务，返回**模型对象**（已 `flush`，`id` 可用）。

        `doc_kind` 与 `kind` 分开：`kind` 是「做了什么」，`doc_kind` 是「写到哪」。
        `optimize` / `clarify` 这类动作凭 `kind` 定不了产物，只有请求体知道；
        创建时就把结论落列，刷新 / 进程重启后仍能回答「这个任务改的是哪一份」。

        `chat_stage` 同理：`kind='chat'` 定不了「哪条线程」，而并发判重与重连都要它。

        `request_params` / `config_snapshot` / `skill_refs` 是任务单里**不可变数据**
        的完整投影：建任务这一刻一次写全，之后任何运行态写入路径都不碰它们。
        供应商四列也从 `config_snapshot["active_provider"]` 这**同一份**定格里取，
        收尾不再从 `usage` 那份 JSON 派生第二遍（配置会被改名/删除，回查等于让
        历史账单跟着今天的状态变）。
        """
        request_text, config_text, skills_text = _dump_immutable(
            request_params, config_snapshot, skill_refs
        )
        # 四列的键名与 `provider` 字典一一对应，避免两处各写一遍映射。
        active = (config_snapshot or {}).get("active_provider") or {}
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
            request_params=request_text,
            config_snapshot=config_text,
            skill_refs=skills_text,
            skill_manifest=_skill_manifest(skill_refs),
            **_provider_columns(active),
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
        `session_scope()`），不要与其它写合并——        长事务会持锁，把生成链路卡住。
        """
        sections_text = _dump_sections(completed_sections)
        _ensure_progress_texts(draft_content, sections_text)
        self.session.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job_id, GenerationJob.owner_id == owner_id)
            .values(
                draft_content=draft_content,
                completed_sections=sections_text,
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
        partial: bool = False,
        lease_token: str = "",
        now_text: str = "",
    ) -> int:
        """标记任务完成，落最终正文与章节。清空错误字段。返回**改到的行数**。

        **条件写**：`WHERE status='running'`（#79）。只有仍在执行的任务才允许收尾成功；
        已经终态的行改到 0 行——调用方据此丢弃本次结果（而不是把第二次收尾又写一遍，
        例如把 `failed` 覆回 `completed`）。

        **执行权三条件（#82）**：调用方给出本轮凭证时，再叠两条数据库层的判据——
        `lease_token` 仍是本轮那个、`lease_expire_at` 还没过。三条合起来只回答一个问题：
        「我现在还有没有资格写这一行」。改到 0 行 = 结果丢弃（丢弃日志与「不重试」的口径
        在 `job_state`，本层只回行数）。

        收尾成功同时**交还执行权**（三列清空）：这一轮结束了，没有谁还在持有它。

        `prompt_tokens` / `completion_tokens` 是 `usage` 那份 JSON 的派生副本：
        管理员后台统计消耗时用 SQL 直接求和，不必把每行 JSON 取回解析。

        供应商四列**不在这里写**：它们已在建任务时从 `config_snapshot` 那份定格
        一次写全，收尾再写一遍等于从 `summary` 另算一条路径。

        `partial` 是「有内容可用、但不完整」（票 #85）：章节重试用尽后照常收尾的
        「部分完成」也走本方法，标记与终态同一条语句落下。
        """
        sections_text = _dump_sections(completed_sections)
        _ensure_progress_texts(draft_content, sections_text)
        conditions: list[Any] = [
            GenerationJob.id == job_id,
            GenerationJob.owner_id == owner_id,
            GenerationJob.status == STATUS_RUNNING,
        ]
        if lease_token:
            # 判权：凭证是本轮那个、到期时间还没过。两列一起进 WHERE 而不是先查后判，
            # 数据库在改之前会重新判一次条件（并发的接管因此不可能被这次收尾覆盖）。
            conditions.append(GenerationJob.lease_token == lease_token)
            conditions.append(GenerationJob.lease_expire_at >= (now_text or utc_now_text()))
        result = self.session.execute(
            update(GenerationJob)
            .where(*conditions)
            .values(
                status=STATUS_COMPLETED,
                draft_content=draft_content,
                completed_sections=sections_text,
                usage=usage,
                error_code="",
                error_message="",
                pending_items=_dump_pending_items(pending_items),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                partial=partial,
                lease_owner="",
                lease_token="",
                lease_expire_at="",
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

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
        partial: bool = False,
        abort_code: str = "",
        lease_token: str = "",
        now_text: str = "",
    ) -> int:
        """标记任务失败，保留已产出的半截正文、章节与已发生的用量。返回**改到的行数**。

        **条件写（#79）**，期望条件分两种：

        - 普通失败 / 成功：`WHERE status='running'`——只允许从执行中收尾。
        - `abort_code` 非空（`CANCELLED` / `SUPERSEDED`）：`status='running'` **或者**
          「已经被同一个 abort 码落库、且 `usage` 还是空」。取消 / 顶替先把行落成终态
          （用户按下停止要马上看得见），那一刻还没有用量；被取消的那条协程醒来收尾时
          要补的正是半截正文与用量。这条「补一次」的条件**只能命中一次**（补完之后
          `usage` 不再是空），所以重复收尾依然是 0 行——而不是「状态是 failed 就随便写」。

        别把条件放宽成「`status='failed'` 一律可写」：那正是「晚到的失败把启动回收写的
        `INTERNAL_ERROR` 覆写成别的东西」的入口。

        **执行权三条件（#82）**：调用方给出本轮凭证时，普通失败再叠「凭证是本轮 + 没到期」，
        改到 0 行即丢弃。`abort_code`（取消 / 被顶替之后补一次载荷）那条路只叠**凭证**、
        不叠到期：那两列回答的是「还能不能被别人领走」，而终态行不会再被任何人领走
        （抢占要求 `status='running'`），所以对它写权的唯一判据是「这一轮是不是你的」
        ＋「载荷只补一次」（`usage IS NULL`）。这不是放宽：#79 的「补一次载荷」契约
        必须保留（用户按停止之后，已经烧掉的 token 与半截正文仍要记账），而旧凭证
        在凭证判据上照样一行都改不到。

        `usage` 在失败路径同样要落：失败的生成**照样消耗了 token**，
        只记成功那次等于把最贵的失败藏起来。两个 token 列同理一并写：
        成本统计若跳过失败任务，「这次为什么这么贵」永远查不出来。

        供应商四列**不在这里写**：它们已在建任务时从同一份 `config_snapshot`
        定格，收尾不再从 `summary` 另算一遍。

        `partial` 是「失败但有内容可用」（票 #85）：失败收尾把已生成的正文落成
        `document_versions.partial=true` 时，任务行同一次事务里留这一份投影。
        """
        sections_text = _dump_sections(completed_sections)
        _ensure_progress_texts(draft_content, sections_text)
        if abort_code:
            expected: Any = or_(
                GenerationJob.status == STATUS_RUNNING,
                and_(
                    GenerationJob.status == STATUS_FAILED,
                    GenerationJob.error_code == abort_code,
                    GenerationJob.usage.is_(None),
                ),
            )
        else:
            expected = GenerationJob.status == STATUS_RUNNING
        conditions: list[Any] = [
            GenerationJob.id == job_id,
            GenerationJob.owner_id == owner_id,
            expected,
        ]
        if lease_token:
            conditions.append(GenerationJob.lease_token == lease_token)
            if not abort_code:
                conditions.append(
                    GenerationJob.lease_expire_at >= (now_text or utc_now_text())
                )
        result = self.session.execute(
            update(GenerationJob)
            .where(*conditions)
            .values(
                status=STATUS_FAILED,
                error_code=error_code,
                error_message=error_message,
                draft_content=draft_content,
                completed_sections=sections_text,
                usage=usage,
                pending_items=_dump_pending_items(pending_items),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                partial=partial,
                lease_owner="",
                lease_token="",
                lease_expire_at="",
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # 取消 / 顶替（条件更新，返回 rowcount）
    # ------------------------------------------------------------------

    def supersede_active(
        self,
        owner_id: int,
        *,
        solution_id: int | None,
        kind: str,
        error_code: str,
        error_message: str,
    ) -> int:
        """把同 owner + solution + kind 下旧的 running 置为 failed，返回被取代的条数。

        「同一 owner + solution + kind 至多一个 running」是一条业务不变量，在新建任务前
        先把旧的判为被取代，避免两个任务同时往同一处写正文。
        `solution_id` 可能为 `None`，SQL 需用 `IS NULL` 而不是 `= NULL`。

        `error_code` 用 `SUPERSEDED` 而不是笼统的 `INTERNAL_ERROR`：这次失败有明确原因
        （被别人接手了，不是出错），运维看这一行时不该去翻日志找异常。取值由
        `job_state` 统一给出，本层不再各写一份字面量（#79）。
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
                error_code=error_code,
                error_message=error_message,
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    def cancel_one(
        self, owner_id: int, job_id: int, *, error_code: str, error_message: str
    ) -> bool:
        """把某一条 running 任务置为 failed + 入参错误码，返回是否确实改到了。

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
                error_code=error_code,
                error_message=error_message,
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0) == 1

    def cancel_running(
        self, owner_id: int, *, solution_id: int, error_code: str, error_message: str
    ) -> int:
        """把该 owner + 方案下所有 running 任务置为 failed，返回受影响条数。

        用在「方案被删除」：方案没了，挂在它上面的生成任务再跑完也没地方落正文，
        继续跑只会白烧 token。

        ⚠️ **刻意复用 `failed` 而不是新造一个 `cancelled` 状态**：任务状态是前后端共享
        的契约（前端所有 status 分支都按 running / completed / failed 三态判），
        多一个取值意味着每一处分支都要多一条分支，而漏掉任何一处都会让界面停在
        「生成中」。语义上被取消确实「没有成功」，复用 `failed` 是代价最小的选择；
        具体原因放在 `error_code` / `error_message` 里（取值由 `job_state` 统一给出，
        本层不再各写一份字面量，#79）。

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
                error_code=error_code,
                error_message=error_message,
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    def cancel_running_for_owner(
        self, owner_id: int, *, error_code: str, error_message: str
    ) -> int:
        """把该 owner 名下**所有** running 任务置为 failed + 入参错误码，返回条数。

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
                error_code=error_code,
                error_message=error_message,
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # 恢复（租约过期，票 #83）：扫候选 / 回排队 / 判失败
    # ------------------------------------------------------------------

    def scan_recoverable(
        self, *, now_text: str, limit: int, startup: bool = False
    ) -> list[GenerationJob]:
        """系统级维护读：扫一批该接手的 running 行（返回模型对象）。

        本仓库两个系统级维护读之一（另一个是抢占侧的 `claimable`）：恢复天然跨用户。
        条件分两档（语义与去向见 `services/job_recovery` 的模块 docstring）：

        周期档（`startup=False`，默认）三句，各有各的必要性：

        - `status='running'`：只有执行中才谈得上「接手」（#79 的合法前态）。
        - `lease_expire_at != ''`：**没有租约的行是「排队中」，不是孤儿**——它本来就
          该等调度循环来领，恢复器去动它等于把排队中的活判死。
        - `lease_expire_at < now_text`：到期才轮得到别人接手。时间用 ISO 文本直接比，
          字典序即时间序（`core/clock.py` 的口径），不把时间取回 Python 再判。

        启动档（`startup=True`）只留 `status='running'` 一句：进程刚起来时它上面
        不可能有真正的执行者（单 worker 硬约束，`_RUNTIMES` 在进程内），库里任何
        `running` 行都是上次进程留下的——**包括还没到期的租约**（不看的话「崩溃前
        1 秒领走」的行要再等满一个租约时长才被动），**也包括从没被领过的残留行**
        （登记完还没派发就崩了；本票没有调度循环，留着就是永远进行中）。

        排序 `lease_expire_at, id`：过期最久的最先收，同一秒的按自增 id 稳定排序
        （启动档里没租约的行 `lease_expire_at` 是空串，字典序最小，同样稳定地排在
        前面）。只挑候选，不判权——真正的「改不改得到」由 `requeue_lease` /
        `fail_expired` 的条件写回答。
        """
        conditions: list[Any] = [GenerationJob.status == STATUS_RUNNING]
        if not startup:
            conditions.append(GenerationJob.lease_expire_at != "")
            conditions.append(GenerationJob.lease_expire_at < now_text)
        return list(
            self.session.execute(
                select(GenerationJob)
                .where(*conditions)
                .order_by(GenerationJob.lease_expire_at, GenerationJob.id)
                .limit(max(1, int(limit)))
            )
            .scalars()
            .all()
        )

    def requeue_lease(
        self,
        owner_id: int,
        job_id: int,
        *,
        lease_token: str,
        lease_expire_at: str,
        now_text: str = "",
        next_run_at: str = "",
    ) -> int:
        """恢复器：把过期租约交还队列（**状态不变**，只清租约三列），返回改到的行数。

        ## 为什么是条件写

        `WHERE` 里带的是**扫描时读到的旧凭证**（`lease_token` 与 `lease_expire_at`
        两列一起），不是「刚才查到的行」。数据库在改之前会重新判一次条件，于是两种
        竞争都改到 0 行：凭证被换掉（别人已经接手）、到期时间被推后（执行者其实还在
        跑，刚续了租）。恢复器凭的就是「这一行还是我扫描时看到的那一份死租约」——
        跟一个晚到的执行者拿旧凭证写结果时遵守的是同一条规则。

        ## 清三列 = 回排队

        `lease_owner / lease_token / lease_expire_at` 全清空之后，这一行在
        `claimable` 眼里就是「还没人领」，下一个执行者能直接领走（#82 的抢占条件
        正是 `lease_token='' or lease_expire_at < now`）。`updated_at` 一起更新：
        「回排队就排到同优先级队尾」是既有排序语义。

        ## `next_run_at`：回排队**不等于立刻能领**（#88）

        失败退避把「最早可再被领走的时刻」一起写进这一行（取值来自
        `services/job_backoff` 的唯一映射表，由状态机按错误码算好后传入）：
        到期之前 `claim` / `claimable` 的到期闸门（`_due_now`）都过不去。
        它与清租约、`updated_at`、账本那一笔在**同一条 UPDATE、同一个事务**里完成
        ——「先回排队、再单独补一条退避时间」会留下一个能被立刻领走的窗口，
        连续限流就会在那几十毫秒里把重试次数烧光。

        ## 为什么不在这里改状态

        回排队**不是**一次状态转换（`running` 还是 `running`），改状态只能经状态机；
        本方法只回答「这一次交接成没成」。账本那一笔由调用方（`job_state.transition`
        的 `REQUEUE` 分支）与它同事务写入，所以这里不写任何结论。
        """
        result = self.session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.owner_id == owner_id,
                GenerationJob.id == job_id,
                GenerationJob.status == STATUS_RUNNING,
                GenerationJob.lease_token == lease_token,
                GenerationJob.lease_expire_at == lease_expire_at,
            )
            .values(
                lease_owner="",
                lease_token="",
                lease_expire_at="",
                next_run_at=next_run_at,
                updated_at=now_text or utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    def fail_expired(
        self,
        owner_id: int,
        job_id: int,
        *,
        lease_token: str,
        lease_expire_at: str,
        error_code: str,
        error_message: str,
        now_text: str = "",
    ) -> int:
        """恢复器：把过期租约**判失败**（终态 + 清租约），返回改到的行数。

        与 `requeue_lease` 同一套 fencing（旧凭证 + 旧到期时间两列进 `WHERE`），
        区别只在写入的结局：`failed + 入参错误码`（超时用 `LEASE_EXPIRED`）+ 清租约。

        **不复用 `fail`**：那个方法叠的是「这一轮凭证还是我的、且**还没到期**」这条
        判据（#82 的写权口径）。恢复器面对的恰恰是**已经过期**的租约，拿那条条件写
        永远 0 行；而 `fail` 的 `abort_code` 那条放宽分支只服务于「取消之后补一次
        载荷」，也不该被超时复用来绕开判权。两个场景的期望条件不同，所以是两个方法。

        不写 `draft_content` / `usage`：恢复器手里没有本轮产物（执行者已经不在场），
        行上已有的半截进度原样保留，用量保持「未知」而不是伪造成 0。
        """
        result = self.session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.owner_id == owner_id,
                GenerationJob.id == job_id,
                GenerationJob.status == STATUS_RUNNING,
                GenerationJob.lease_token == lease_token,
                GenerationJob.lease_expire_at == lease_expire_at,
            )
            .values(
                status=STATUS_FAILED,
                error_code=error_code,
                error_message=error_message,
                lease_owner="",
                lease_token="",
                lease_expire_at="",
                updated_at=now_text or utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # 执行权（租约，票 #82）：抢占 / 续租 / 扫待领
    # ------------------------------------------------------------------

    def claim(
        self,
        owner_id: int,
        job_id: int,
        *,
        lease_owner: str,
        lease_token: str,
        expire_at: str,
        now_text: str,
    ) -> int:
        """抢占一条任务，返回**改到的行数**（1 = 领到了，0 = 没领到）。

        ## 判断与修改压在同一条 UPDATE 里

        `WHERE` 里的「没有有效租约」是这条语句的核心：`lease_token = ''`（从没人领过）
        **或** `lease_expire_at < 现在`（领它的人已经到期）。数据库在改之前会重新判一次
        条件，所以两个进程同时执行同一条语句时，第一个改到的那些行对第二个已经不满足
        条件——它一行都改不到。**谁改到了行，谁才算领到**；这不是靠错开时间去避让。

        反过来说，写成「先查有没有人领、再写」就一定会漏：查与写之间那段窗口里，
        两边都会读到「还没人领」，然后都以为自己领到了（后写的那个把先写的凭证覆盖掉，
        于是两个执行者同时在跑同一行）。

        ## 允许领「已过期」的行

        这是崩溃接手的入口（#83 的恢复器走的就是它）：原执行者的进程没了，租约到期，
        另一处用新的凭证接手。凭什么接手者更有权？因为它也守同一条规则——凭证对不上，
        它一样一行都改不到。

        `lease_owner` 只写进库供排查，**不参与任何条件**：判权的是凭证。

        ## 再加一条「到期闸门」（#88）

        `next_run_at` 是失败退避写下的「最早可再被领走的时刻」：未到期时这条 UPDATE
        一行都改不到，于是「回排队了但还要等一会儿」不是靠调用方自律，而是数据库层的
        机械强制（AC2）。判据本身只有一处定义（`_due_now`）。
        """
        result = self.session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.owner_id == owner_id,
                GenerationJob.id == job_id,
                GenerationJob.status == STATUS_RUNNING,
                or_(
                    GenerationJob.lease_token == "",
                    GenerationJob.lease_expire_at < now_text,
                ),
                # 失败退避：还没到期的一行领不走（`next_run_at=''` = 立即）。
                _due_now(now_text),
            )
            .values(
                lease_owner=lease_owner,
                lease_token=lease_token,
                lease_expire_at=expire_at,
                # 与判据同一个时刻：`now_text` 是可注入的「现在」（`LeaseGrant` 带着它），
                # 写成墙钟会让「在注入时刻领走」的行在队列里排到别的秒次上（#88 的
                # 排序规则要看得懂这一步的 `updated_at`）。
                updated_at=now_text or utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    def renew_lease(
        self,
        owner_id: int,
        job_id: int,
        *,
        lease_token: str,
        expire_at: str,
    ) -> int:
        """续租：把到期时间往后推，返回**改到的行数**（0 = 已失去执行权）。

        条件只有两条：状态仍是 `running`、凭证仍是本轮那个。**刻意不叠加「还没到期」**——
        续租本来就是为了把到期时间推后；如果要求未到期，一次比租约时长更久的模型调用
        就会让执行者把自己判死（长任务在动作点之间停多久不由它自己决定）。
        到期这条判据约束的是**写结果**（见 `finish` / `fail`），不是「还能不能继续干」。

        反过来，续租失败**本身就是信号**：凭证对不上说明这一轮已经被取消或被接手，
        调用方该立刻停下来（`job_claimer.renew` 会把它翻成一个明确的错误）。
        """
        result = self.session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.owner_id == owner_id,
                GenerationJob.id == job_id,
                GenerationJob.status == STATUS_RUNNING,
                GenerationJob.lease_token == lease_token,
            )
            .values(lease_expire_at=expire_at, updated_at=utc_now_text())
        )
        return int(result.rowcount or 0)

    def claimable(self, *, now_text: str, limit: int) -> list[tuple[int, int]]:
        """系统级维护：扫一批「还没有有效租约」的待领任务，返回 `(owner_id, id)`。

        本仓库两个系统级维护读之一（另一个是恢复器的 `scan_recoverable`）：队列扫描
        天然跨用户。
        它**只挑候选**，判权仍由 `claim` 那条条件写完成——扫描结果过期（被别的进程
        先领走）不会造成两个执行者，`claim` 会如实回 0 行。

        排序规则（AC3，唯一一处定义在 `_queue_order`）：**先过到期闸门**（没到
        `next_run_at` 的行直接不是候选，见 `_due_now`），**到期之后按 `updated_at`
        排队**，同一秒用自增 `id` 稳定。`updated_at` 是「回排队就排到队尾」那条既有
        语义；`created_at` 不再参与——建得早不等于该先跑。
        """
        rows = self.session.execute(
            select(GenerationJob.owner_id, GenerationJob.id)
            .where(
                GenerationJob.status == STATUS_RUNNING,
                or_(
                    GenerationJob.lease_token == "",
                    GenerationJob.lease_expire_at < now_text,
                ),
                # 到期闸门（#88）：没到 `next_run_at` 的行不是候选，与 `claim` 同一处定义。
                _due_now(now_text),
            )
            .order_by(*_queue_order())
            .limit(max(1, int(limit)))
        ).all()
        return [(int(row[0]), int(row[1])) for row in rows]

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

    def find_dedupe_target(
        self,
        owner_id: int,
        *,
        solution_id: int | None,
        kind: str,
        doc_kind: str,
        input_fingerprint: str,
        created_since: str,
    ) -> GenerationJob | None:
        """查「这次提交可以复用哪一条既有任务」（创建去重，票 #87）。

        判据是**同键 + 仍在 running + 在时间窗内**：键为 owner / 方案 / `kind` /
        `doc_kind` / 输入指纹；`created_since` 是窗口下界的时间文本。

        `created_at` 是 `timestamp_text()`（ISO 文本），字典序即时间序，因此
        `>=` 可以直接在 SQL 里比（见 `core/clock.py` 的口径）——不必把时间取回
        Python 再判断。`solution_id` 可能为 `None`（没有方案的任务），用 `IS NULL`。

        只返回 `running`：终态任务不复用（生成已结束，再点一次是「重新生成」）。
        排序取最近一条：同一键在窗内理论上至多一条（登记互斥保证），`id` 兜底。
        """
        conditions = [
            GenerationJob.owner_id == owner_id,
            GenerationJob.kind == kind,
            GenerationJob.doc_kind == doc_kind,
            GenerationJob.input_fingerprint == input_fingerprint,
            GenerationJob.status == STATUS_RUNNING,
            GenerationJob.created_at >= created_since,
        ]
        conditions.append(
            GenerationJob.solution_id.is_(None)
            if solution_id is None
            else GenerationJob.solution_id == solution_id
        )
        return (
            self.session.execute(
                select(GenerationJob)
                .where(*conditions)
                .order_by(GenerationJob.id.desc())
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
