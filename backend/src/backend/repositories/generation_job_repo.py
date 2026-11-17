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
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlmodel import Session

from backend.core.clock import utc_now_text
from backend.core.limits import ensure_text_within_limit
from backend.core.models import GenerationJob, User

# 任务状态：七值（票 #84）。**这是全仓唯一的取值定义**——服务层、管理端统计、
# 对外 API 都从这一处取，不在别处再拼一份字面量。
#
# 七个状态一句话：
# - `pending`（排队中）：已接单、没有执行者；可被领走，也可被直接取消；
# - `running`（执行中）：某个执行者正拿着这条任务的租约在跑；
# - `cancelling`（取消中）：用户已经说要停，等执行者确认退出；
# - `succeeded`（已定稿）：正文已经可靠存进库；
# - `failed`（失败）：真失败（含部分完成），**不是**「没跑成」的统称；
# - `superseded`（被取代）：同一份文档有更新的任务接手，这一支让位（系统行为）；
# - `cancelled`（已取消）：用户取消 / 方案被删（用户意图）。
#
# 「被取代」与「已取消」必须分开：两者都没有结果，含义却相反。合并之后排查时
# 永远分不清「是用户不要了，还是被新任务挤掉了」——这也正是统计口径要分开的原因。
#
# ⚠️ 不再有 `completed` 这个取值：它的语义由 `succeeded` 承担。也不再有
# 「`running` + 没有租约 = 排队中」这种同形表达——「排队中」只有 `pending` 一个表示。
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_CANCELLING = "cancelling"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"
STATUS_CANCELLED = "cancelled"

# 在途（还没收尾）与终态（不会再变）两套集合。它们互斥且覆盖全部七个值：
# 任何「是不是在飞 / 是不是已收尾」的判断都从这两处取，不各自列举。
ACTIVE_STATUSES: tuple[str, ...] = (STATUS_PENDING, STATUS_RUNNING, STATUS_CANCELLING)
TERMINAL_STATUSES: tuple[str, ...] = (
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_SUPERSEDED,
    STATUS_CANCELLED,
)
# 「会被重复提交复用」的目标：排队中 / 执行中（取消中的任务正在退出，不该被复用）。
REUSABLE_STATUSES: tuple[str, ...] = (STATUS_PENDING, STATUS_RUNNING)

# 卡死判定的「停更」阈值（票 #89 从管理端服务层移到仓储层，成为唯一一处定义）。
#
# 为什么是 30 分钟：`updated_at` 在流式写入与动作点续租时都会刷新，它停住不动
# 恰恰说明「不再产出」。半小时足以覆盖长文档里最慢的单步生成；再短会把正常的长任务
# 误报成卡死，管理员对红色标记就会脱敏。
STUCK_MINUTES = 30


def stuck_condition(*, now_text: str) -> Any:
    """**卡死判据的唯一一处定义**（票 #89）：执行中，且出现下面任一情形。

    - **租约已过期**（`lease_expire_at` 非空且早于 `now_text`）：执行者本该续租却没续。
      七态之后这是「仍 `running` 但已经没人有权写它」的窗口——#83 的恢复器会把它
      收口（回排队 / 判失败），但在恢复器跑到之前，它必须算卡死，否则卡死数永远是 0。
    - **停更超过 `STUCK_MINUTES`**：没有租约或租约还活着、但进度不再刷新。租约与
      进度都由同一个动作点刷新，两者通常同向；保留这条是为了覆盖「没有租约的
      `running` 残留行」与「续租了但不再产出」两种历史形态。

    ⚠️ 只认 `running`：`pending` 本来就在排队，`cancelling` 是用户已经叫停、
    由执行者或恢复器收口——都不是「卡住没人管」。

    ⚠️ 时间比较全部是**ISO 文本的字典序**（`core/clock.py` 的口径），不把时间取回
    Python 再比：聚合查询（`admin_repo.job_metrics`）与列表筛选（`admin_list`）
    共用这一个表达式，两处各写一遍就会出现「卡片说 1 个、列表里 0 个」。

    Args:
        now_text: 判据时刻（`utc_now_text()` 的形态，不带时区标记）。
    """
    stale_before = (datetime.fromisoformat(now_text) - timedelta(minutes=STUCK_MINUTES)).isoformat()
    return and_(
        GenerationJob.status == STATUS_RUNNING,
        or_(
            and_(
                GenerationJob.lease_expire_at != "",
                GenerationJob.lease_expire_at < now_text,
            ),
            GenerationJob.updated_at < stale_before,
        ),
    )


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
    """管理面投影：任务模型 + JOIN 出来的用户名 + 服务端算好的卡死标记。

    `owner_name` 来自 `users.username`；`is_stuck` 是**查询时由 SQL 判据算出来的**
    （`stuck_condition`），不是服务层再猜一遍：管理端卡片与列表必须同一处定义，
    否则会出现「卡片说 1 个卡死、列表里 0 个」。`as_dict()` 把模型全列、用户名与
    这个标记合并成管理员视图需要的扁平 dict（`_job_view` 消费的就是它）。
    """

    job: GenerationJob
    owner_name: str
    # 是否卡死（由 `stuck_condition` 在查询时算好；票 #89 起前端只展示不判断）。
    is_stuck: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {**self.job.model_dump(), "owner_name": self.owner_name, "is_stuck": self.is_stuck}


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
        """新建一条**排队中**任务，返回**模型对象**（已 `flush`，`id` 可用）。

        票 #84 起建行即 `pending`：登记只代表「接单了」，谁去跑由抢占那条转换回答
        （`claim` 才把它改成 `running` 并写租约）。这样「排队中」有唯一的表示，
        不再靠「`running` + 没有有效租约」同形表达——后者永远分不清「刚建好」与
        「被恢复器交还队列」。

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
            status=STATUS_PENDING,
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
        completed_sections: list[dict[str, Any]] | None = None,
        phase: str,
        pending_items: list[dict[str, Any]] | None = None,
    ) -> None:
        """合并写一次进度（正文 / 章节 / 阶段 / 待确认清单）。WHERE 含 owner_id，改不到别人的任务。

        高频写（≤300ms / 增量 4KB）：**每次调用仍应是一个短事务**（调用方每次一个
        `session_scope()`），不要与其它写合并——        长事务会持锁，把生成链路卡住。
        """
        sections_text = _dump_sections(completed_sections or [])
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
        completed_sections: list[dict[str, Any]] | None = None,
        usage: str | None = None,
        pending_items: list[dict[str, Any]] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        partial: bool = False,
        lease_token: str = "",
        now_text: str = "",
    ) -> int:
        """标记任务**已定稿**（`succeeded`），落最终正文与章节。清空错误字段。返回改到的行数。

        **条件写**：`WHERE status='running'`（#79）。只有仍在执行的任务才允许收尾成功；
        已经终态的行改到 0 行——调用方据此丢弃本次结果（而不是把第二次收尾又写一遍，
        例如把 `failed` 覆回 `succeeded`）。

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
        sections_text = _dump_sections(completed_sections or [])
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
                status=STATUS_SUCCEEDED,
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
        completed_sections: list[dict[str, Any]] | None = None,
        usage: str | None = None,
        pending_items: list[dict[str, Any]] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        partial: bool = False,
        lease_token: str = "",
        now_text: str = "",
    ) -> int:
        """标记任务**失败**（`failed`），保留已产出的半截正文、章节与已发生的用量。

        **条件写（#79）**：`WHERE status='running'`——只允许从执行中失败收尾。
        已经终态的行改到 0 行，调用方据此丢弃本次结果。**别把条件放宽成**
        「`status='failed'` 一律可写」：那正是「晚到的失败把启动回收写的
        `INTERNAL_ERROR` 覆写成别的东西」的入口。

        「取消 / 被取代」不在这里：它们有各自的状态（`cancelled` / `superseded`），
        走 `confirm_cancel` / `confirm_superseded` 两条转换（票 #84）。

        `usage` 在失败路径同样要落：失败的生成**照样消耗了 token**，
        只记成功那次等于把最贵的失败藏起来。两个 token 列同理一并写：
        成本统计若跳过失败任务，「这次为什么这么贵」永远查不出来。

        供应商四列**不在这里写**：它们已在建任务时从同一份 `config_snapshot`
        定格，收尾不再从 `summary` 另算一遍。

        `partial` 是「失败但有内容可用」（票 #85）：失败收尾把已生成的正文落成
        `document_versions.partial=true` 时，任务行同一次事务里留这一份投影。
        """
        sections_text = _dump_sections(completed_sections or [])
        _ensure_progress_texts(draft_content, sections_text)
        conditions: list[Any] = [
            GenerationJob.id == job_id,
            GenerationJob.owner_id == owner_id,
            GenerationJob.status == STATUS_RUNNING,
        ]
        if lease_token:
            conditions.append(GenerationJob.lease_token == lease_token)
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
    # 取消 / 顶替（票 #84：请求与确认分开，条件更新返回 rowcount）
    # ------------------------------------------------------------------
    #
    # 所有者/方案/账号三种批量作用域共用「请求取消」的两个动作，而不是三套：
    # - `_request_cancel`：按交给它的 WHERE 条件，把 `pending` 直接改成 `cancelled`
    #   （没人在跑，不需要等谁确认），把 `running` / `cancelling` 改成 `cancelling`
    #   （执行者要自己收口）。**两条 UPDATE 相加**才是这次请求影响的条数。
    # 一次请求命中两种去向，所以不能写成一条带 CASE 的 UPDATE 再去判「哪些算」——
    # 那样调用方还得回头猜每行去了哪儿；这里只回行数，去向由随后的读回答。

    def _request_cancel(
        self, conditions: list[Any], *, error_message: str
    ) -> int:
        """对一个作用域发起取消请求：排队中的直接落已取消，执行中的转取消中。"""
        now = utc_now_text()
        immediate = self.session.execute(
            update(GenerationJob)
            .where(*conditions, GenerationJob.status == STATUS_PENDING)
            .values(
                status=STATUS_CANCELLED,
                error_code="",
                error_message=error_message,
                updated_at=now,
            )
        )
        requesting = self.session.execute(
            update(GenerationJob)
            .where(
                *conditions,
                GenerationJob.status.in_((STATUS_RUNNING, STATUS_CANCELLING)),
            )
            .values(
                status=STATUS_CANCELLING,
                error_code="",
                error_message=error_message,
                updated_at=now,
            )
        )
        return int(immediate.rowcount or 0) + int(requesting.rowcount or 0)

    def request_cancel_one(
        self, owner_id: int, job_id: int, *, error_message: str
    ) -> bool:
        """对某一条任务发起取消请求，返回是否确实改到了。

        用户按「停止生成」针对的是**这一次**任务；同一方案下别的线程可能也在跑，
        所以不能顺手批量取消。

        任务已经自然结束（0 行）时返回 False：调用方据此回答「没取消到东西」，
        而不是报错——「你点的时候它刚好说完了」不是失败。
        """
        rows = self._request_cancel(
            [GenerationJob.owner_id == owner_id, GenerationJob.id == job_id],
            error_message=error_message,
        )
        return rows > 0

    def request_cancel_solution(
        self, owner_id: int, *, solution_id: int, error_message: str
    ) -> int:
        """对某方案下所有在途任务发起取消请求（删方案），返回受影响条数。

        方案没了，挂在它上面的生成任务再跑完也没地方落正文，继续跑只是白烧 token。
        `solution_id` 用 `= ?`：本方法只服务于「删掉一份具体方案」，没有方案的
        逃生口任务不会被这个动作波及。
        """
        return self._request_cancel(
            [
                GenerationJob.owner_id == owner_id,
                GenerationJob.solution_id == solution_id,
            ],
            error_message=error_message,
        )

    def request_cancel_owner(self, owner_id: int, *, error_message: str) -> int:
        """对某用户名下**所有**在途任务发起取消请求（删账号），返回条数。

        与按方案的区别只有过滤维度：删账号要覆盖名下所有方案，包括还没方案的
        逃生口任务（`solution_id IS NULL`）。
        """
        return self._request_cancel(
            [GenerationJob.owner_id == owner_id], error_message=error_message
        )

    def confirm_cancel(
        self,
        owner_id: int,
        job_id: int,
        *,
        error_message: str,
        draft_content: str = "",
        completed_sections: list[dict[str, Any]] | None = None,
        usage: str | None = None,
        pending_items: list[dict[str, Any]] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        partial: bool = False,
        lease_token: str = "",
        now_text: str = "",
    ) -> int:
        """**确认取消**：把执行中 / 取消中的任务落成已取消，清租约，返回改到的行数。

        这是「取消」的第二阶段：用户按下停止只把行变成 `cancelling`，真正的终态由
        还有资格写这一行的人落——执行者在下一个动作点看到标记，或恢复器在租约过期
        后兜底（后者走 `cancel_expired`，因为那时执行权凭证已经不作数了）。

        载荷（半截正文 / 用量）与终态**同一条语句**写下来：取消不是「什么都没发生」，
        已经烧掉的 token 照记。条件只有两条——状态仍是参与执行的两个值、凭证仍是
        本轮那个（`lease_token` 给出时）；第二次确认改到 0 行。
        """
        conditions: list[Any] = [
            GenerationJob.owner_id == owner_id,
            GenerationJob.id == job_id,
            GenerationJob.status.in_((STATUS_RUNNING, STATUS_CANCELLING)),
        ]
        if lease_token:
            conditions.append(GenerationJob.lease_token == lease_token)
        result = self.session.execute(
            update(GenerationJob)
            .where(*conditions)
            .values(
                status=STATUS_CANCELLED,
                # 已取消不是错误：`error_code` 只表达失败原因，状态由 `status` 表达。
                error_code="",
                error_message=error_message,
                draft_content=draft_content,
                completed_sections=_dump_sections(completed_sections or []),
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

    def settle_superseded_payload(
        self,
        owner_id: int,
        job_id: int,
        *,
        draft_content: str = "",
        completed_sections: list[dict[str, Any]] | None = None,
        usage: str | None = None,
        pending_items: list[dict[str, Any]] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        partial: bool = False,
    ) -> int:
        """被顶替之后**补一次载荷**（半截正文 / 用量），返回改到的行数。

        顶替是登记路径先落终态（`supersede_active`，此刻还不知道被顶替那一支烧了多少
        token）；被顶替的执行者醒来收尾时，把这一次执行已经产生的正文与用量补上。
        「补一次」由条件收口：`status='superseded'` **且 `usage IS NULL`**——补完之后
        `usage` 不再是空，第二次照样 0 行。终态行不会再被任何人领走，所以不需要凭证。
        """
        result = self.session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.owner_id == owner_id,
                GenerationJob.id == job_id,
                GenerationJob.status == STATUS_SUPERSEDED,
                GenerationJob.usage.is_(None),
            )
            .values(
                draft_content=draft_content,
                completed_sections=_dump_sections(completed_sections or []),
                usage=usage,
                pending_items=_dump_pending_items(pending_items),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                partial=partial,
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    def supersede_active(
        self,
        owner_id: int,
        *,
        solution_id: int | None,
        kind: str,
        error_message: str,
    ) -> int:
        """把同 owner + solution + kind 下旧的**在途**任务置为被取代，返回条数。

        「同一 owner + solution + kind 至多一个在途任务」是一条业务不变量，在新建任务前
        先把旧的判为被取代，避免两个任务同时往同一处写正文。

        覆盖三种前态（`pending` / `running` / `cancelling`）：排队中的让位、执行中的让开、
        正在取消的任务同样已经有更新的任务接手——统一落 `superseded`，不是「取消」：
        它们一个是被新任务挤掉（系统行为），一个是被用户叫停（用户意图），统计上必须分开。

        `error_code` 留空：`SUPERSEDED` 曾经是错误码，现在由状态表达（AC4）。
        `solution_id` 可能为 `None`，SQL 用 `IS NULL` 而不是 `= NULL`。
        """
        conditions = [
            GenerationJob.owner_id == owner_id,
            GenerationJob.kind == kind,
            GenerationJob.status.in_(ACTIVE_STATUSES),
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
                status=STATUS_SUPERSEDED,
                error_code="",
                error_message=error_message,
                # 终态一律清空租约三列：这一行没有执行者了（被顶替的那一支即使还醒着，
                # 也只能补一次载荷，不能借租约继续写）。
                lease_owner="",
                lease_token="",
                lease_expire_at="",
                updated_at=utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    def fail_queued(
        self,
        owner_id: int,
        job_id: int,
        *,
        error_code: str,
        error_message: str,
        draft_content: str = "",
        completed_sections: list[dict[str, Any]] | None = None,
        usage: str | None = None,
        pending_items: list[dict[str, Any]] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        partial: bool = False,
        now_text: str = "",
    ) -> int:
        """启动档专用：把一条**排队中**的行落成失败（服务重启中断）。

        为什么单独一个方法而不是复用 `fail`：`fail` 的前态是 `running`——只有执行者
        才有资格写「这一轮失败了」。把条件放宽到 `pending` 会让「旧执行者的晚到失败」
        写进一条已经被恢复器交还队列的行（那是二阶事故：队列里的活被别人的结论判死）。
        本方法只由启动回收调用（那时代码刚起来，不可能有执行者），前态固定为
        `pending`，改到 0 行即跳过。
        """
        result = self.session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.owner_id == owner_id,
                GenerationJob.id == job_id,
                GenerationJob.status == STATUS_PENDING,
            )
            .values(
                status=STATUS_FAILED,
                error_code=error_code,
                error_message=error_message,
                draft_content=draft_content,
                completed_sections=_dump_sections(completed_sections or []),
                usage=usage,
                pending_items=_dump_pending_items(pending_items),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                partial=partial,
                lease_owner="",
                lease_token="",
                lease_expire_at="",
                updated_at=now_text or utc_now_text(),
            )
        )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # 恢复（租约过期，票 #83）：扫候选 / 回排队 / 判失败
    # ------------------------------------------------------------------

    def scan_recoverable(
        self, *, now_text: str, limit: int, startup: bool = False
    ) -> list[GenerationJob]:
        """系统级维护读：扫一批该接手的**非终态**行（返回模型对象）。

        本仓库两个系统级维护读之一（另一个是抢占侧的 `claimable`）：恢复天然跨用户。
        条件分两档（语义与去向见 `services/job_recovery` 的模块 docstring）：

        周期档（`startup=False`，默认）只看两种「该有人收口却没人收」的行：

        - `status='running'` 且租约已过期：执行者的进程没了（#83 的交接）；
        - `status='cancelling'` 且租约已过期（或租约本来就是空的）：用户要停，但执行者
          卡住没来确认（票 #84 的 AC3）。**取消中先由执行者确认**，恢复器只在租约过期
          之后接手，否则会把一条正在正常收口的任务抢走。
        - `pending` **不是**候选：排队中本来就该等下一次抢占，恢复器去动它等于把排队
          中的活判死。时间用 ISO 文本直接比，字典序即时间序（`core/clock.py` 的口径）。

        启动档（`startup=True`）扫所有非终态行：进程刚起来时它上面不可能有真正的
        执行者（单 worker 硬约束，`_RUNTIMES` 在进程内），库里任何 `running` / `cancelling`
        行都是上次进程留下的——**包括还没到期的租约**（不看的话「崩溃前 1 秒领走」的
        行要再等满一个租约时长才被动），**也包括从没被领过的残留 `pending` 行**
        （登记完还没派发就崩了；本票没有调度循环，留着就是永远排队中）。

        排序 `lease_expire_at, id`：过期最久的最先收，同一秒的按自增 id 稳定排序
        （启动档里没有租约的行 `lease_expire_at` 是空串，字典序最小，同样稳定地排在
        前面）。只挑候选，不判权——真正的「改不改得到」由 `requeue_lease` /
        `fail_expired` / `cancel_expired` 的条件写回答。
        """
        conditions: list[Any] = []
        if not startup:
            # 周期档两条候选：
            # - 执行中且租约已过期（执行者进程没了）；
            # - 取消中且租约已过期（或者租约本来就是空的：没有执行者还拿得住它）——
            #   AC3 的兜底对象。取消中的行**先由执行者自己确认**，恢复器只在租约过期
            #   之后接手，否则会把一条正在正常收口的任务抢走。
            conditions.append(
                or_(
                    and_(
                        GenerationJob.status == STATUS_RUNNING,
                        GenerationJob.lease_expire_at != "",
                        GenerationJob.lease_expire_at < now_text,
                    ),
                    and_(
                        GenerationJob.status == STATUS_CANCELLING,
                        or_(
                            GenerationJob.lease_expire_at == "",
                            GenerationJob.lease_expire_at < now_text,
                        ),
                    ),
                )
            )
        else:
            # 启动档：进程刚起来时它上面不可能有真正的执行者（单 worker 硬约束，
            # `_RUNTIMES` 在进程内），所以**所有非终态行**都要收口：
            # - `running`（含还没到期的租约）：按超时 / 中断判失败；
            # - `cancelling`：按已取消收口（取消是用户的明确意图，不能记成失败）；
            # - `pending`：登记完还没派发就崩了——本票没有调度循环，留着就是永远排队中。
            conditions.append(GenerationJob.status.in_(ACTIVE_STATUSES))
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
        """恢复器：把过期租约交还队列（**回到 `pending`** 并清租约），返回改到的行数。

        ## 为什么是条件写

        `WHERE` 里带的是**扫描时读到的旧凭证**（`lease_token` 与 `lease_expire_at`
        两列一起），不是「刚才查到的行」。数据库在改之前会重新判一次条件，于是两种
        竞争都改到 0 行：凭证被换掉（别人已经接手）、到期时间被推后（执行者其实还在
        跑，刚续了租）。恢复器凭的就是「这一行还是我扫描时看到的那一份死租约」——
        跟一个晚到的执行者拿旧凭证写结果时遵守的是同一条规则。

        ## 清三列 + 回到 `pending` = 回排队

        七态之后「排队中」只有 `pending` 一个表示，所以这里**同时改状态**：
        `lease_owner / lease_token / lease_expire_at` 全清空、`status` 回到 `pending`，
        下一个执行者靠 `claim`（`WHERE status='pending'`）领走。若只清租约不改状态，
        就会出现第二种「排队中」（`running` + 空租约），那是两个真源。
        `updated_at` 一起更新：「回排队就排到同优先级队尾」是既有排序语义。

        ## `next_run_at`：回排队**不等于立刻能领**（#88）

        失败退避把「最早可再被领走的时刻」一起写进这一行（取值来自
        `services/job_backoff` 的唯一映射表，由状态机按错误码算好后传入）：
        到期之前 `claim` / `claimable` 的到期闸门（`_due_now`）都过不去。
        它与清租约、`updated_at`、账本那一笔在**同一条 UPDATE、同一个事务**里完成
        ——「先回排队、再单独补一条退避时间」会留下一个能被立刻领走的窗口，
        连续限流就会在那几十毫秒里把重试次数烧光。

        ## 为什么不在这里改状态

        回排队**是**一次状态转换（`running` → `pending`），但本层只做「这一次交接成没成」
        的条件写；改状态的规则只在状态机一处（`job_state` 的 `REQUEUE` 分支），
        账本那一笔由它与本次写同事务完成，所以这里不写任何结论。
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
                status=STATUS_PENDING,
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
        永远 0 行。两个场景的期望条件不同，所以是两个方法。

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

    def cancel_expired(
        self,
        owner_id: int,
        job_id: int,
        *,
        lease_token: str,
        lease_expire_at: str,
        error_message: str,
        now_text: str = "",
    ) -> int:
        """恢复器：把**卡住的「取消中」**落成已取消（终态 + 清租约），返回改到的行数。

        票 #84 AC3 的落点：用户已经说要停，执行者却卡在模型调用里没来确认；租约过期
        之后由恢复器替它收口。结局是 `cancelled`（用户的明确意图），**不是 `failed`**——
        把「用户不要了」记成一次失败，统计与排查都会读错。

        与 `requeue_lease` / `fail_expired` 同一套 fencing（扫描时读到的旧凭证 + 旧到期
        时间两列进 `WHERE`）：凭证被换掉 / 刚续过租，都一行改不到。
        取消确认没有载荷：执行者已经不在场，它这一次产生的正文与用量无从得知
        （与 `fail_expired` 同一口径——宁可少算，不伪造 0）。
        """
        result = self.session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.owner_id == owner_id,
                GenerationJob.id == job_id,
                GenerationJob.status == STATUS_CANCELLING,
                GenerationJob.lease_token == lease_token,
                GenerationJob.lease_expire_at == lease_expire_at,
            )
            .values(
                status=STATUS_CANCELLED,
                error_code="",
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
        """抢占一条**排队中**任务，返回**改到的行数**（1 = 领到了，0 = 没领到）。

        ## 判断与修改压在同一条 UPDATE 里

        `WHERE status='pending'` 是这条语句的核心（七态之后「排队中」只有这一个表示）。
        数据库在改之前会重新判一次条件，所以两个进程同时执行同一条语句时，第一个改到的
        那些行对第二个已经不满足条件——它一行都改不到。**谁改到了行，谁才算领到**；
        这不是靠错开时间去避让。

        反过来说，写成「先查有没有人领、再写」就一定会漏：查与写之间那段窗口里，
        两边都会读到「还没人领」，然后都以为自己领到了（后写的那个把先写的凭证覆盖掉，
        于是两个执行者同时在跑同一行）。

        ## 过期租约的「执行中」行不在这里接手

        #82 当时允许直接领「租约已过期」的 `running` 行；七态之后这条路收口到恢复器
        （#83）：过期租约先由 `requeue_lease` 变成 `pending`（并按剩余次数决定去向，
        账本记一笔），再由本方法领走。直接抢一条过期 `running` 行等于绕过恢复器的去向
        判定与账本，等于给「执行中 → 执行中」开了第二个入口。

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
                GenerationJob.status == STATUS_PENDING,
                # 失败退避：还没到期的一行领不走（`next_run_at=''` = 立即）。
                _due_now(now_text),
            )
            .values(
                status=STATUS_RUNNING,
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
        """系统级维护：扫一批**排队中**的待领任务，返回 `(owner_id, id)`。

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
                GenerationJob.status == STATUS_PENDING,
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
        """查该 owner 下最近的**在途**任务（`updated_at DESC`，七态下 = 排队中/执行中/取消中）。

        `solution_id` / `kind` / `chat_stage` 为 `None` 时不参与过滤。
        """
        conditions = [
            GenerationJob.owner_id == owner_id,
            GenerationJob.status.in_(ACTIVE_STATUSES),
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

        判据是**同键 + 仍在排队/执行中 + 在时间窗内**：键为 owner / 方案 / `kind` /
        `doc_kind` / 输入指纹；`created_since` 是窗口下界的时间文本。

        `created_at` 是 `timestamp_text()`（ISO 文本），字典序即时间序，因此
        `>=` 可以直接在 SQL 里比（见 `core/clock.py` 的口径）——不必把时间取回
        Python 再判断。`solution_id` 可能为 `None`（没有方案的任务），用 `IS NULL`。

        只返回 `pending` / `running`：终态任务不复用（生成已结束，再点一次是「重新生成」）；
        `cancelling` 也不复用——那一支正在退出，复用它等于把用户刚叫停的任务当成新活。
        排序取最近一条：同一键在窗内理论上至多一条（登记互斥保证），`id` 兜底。
        """
        conditions = [
            GenerationJob.owner_id == owner_id,
            GenerationJob.kind == kind,
            GenerationJob.doc_kind == doc_kind,
            GenerationJob.input_fingerprint == input_fingerprint,
            GenerationJob.status.in_(REUSABLE_STATUSES),
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
        statuses: tuple[str, ...] | None = None,
        solution_id: int | None = None,
        kind: str | None = None,
    ) -> list[GenerationJob]:
        """列出该 owner 的任务，最近创建在前；可选按**状态集合** / solution_id / kind 过滤。

        `statuses` 是一组取值（对外 `?status=pending,running,cancelling` 的「在途」查询
        就靠它）：空元组 / `None` 表示不筛。不在这里接单个字符串再拼一次——多值过滤
        只有这一个入口，避免「单值走一条 SQL、多值走另一条」的两套实现。
        """
        conditions = [GenerationJob.owner_id == owner_id]
        if statuses:
            conditions.append(GenerationJob.status.in_(statuses))
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
        statuses: tuple[str, ...] | None = None,
        username: str | None = None,
        kind: str | None = None,
        stuck_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AdminJob]:
        """列出任务（管理员视角，JOIN 出用户名），返回 `AdminJob` 投影。

        `statuses` 与用户面同口径：一组取值（可以是七个值里的任意几个）。管理端的
        「在飞」标签传 `pending,running,cancelling`——一列状态值已经能回答「哪些还没收尾」，
        不必再按「有没有租约」猜。

        `stuck_only`（票 #89）：只留**卡死**的行，判据是仓储层的 `stuck_condition`
        ——与概览页「卡死数」用的是同一个表达式。管理员点概览上的卡死数就落到这里，
        两边数字对不上会让人不信任何一个。

        排序按状态分开：
        - 在途（`pending` / `running` / `cancelling`）用 `created_at ASC`——卡死任务的特征
          就是「跑得最久」，把它顶到最前面才看得见；
        - 卡死列表用 `updated_at ASC`——停得最久的最先出现（它们都已经卡住了，
          这时候该看的是「谁卡得最久」而不是「谁跑得久」）；
        - 其余用 `updated_at DESC`——最近有动静的在前。

        `username` 是**模糊匹配**（`LIKE`）：管理员通常只记得半个用户名，
        要求精确输入等于让他先去用户页复制一遍。
        """
        conditions: list[Any] = []
        if statuses:
            conditions.append(GenerationJob.status.in_(statuses))
        if kind:
            conditions.append(GenerationJob.kind == kind)
        if username:
            conditions.append(User.username.like(f"%{username}%"))
        stuck = stuck_condition(now_text=utc_now_text())
        if stuck_only:
            conditions.append(stuck)

        if stuck_only:
            order = (GenerationJob.updated_at.asc(), GenerationJob.id.asc())
        elif statuses and all(value in ACTIVE_STATUSES for value in statuses):
            order = (GenerationJob.created_at.asc(), GenerationJob.id.asc())
        else:
            order = (GenerationJob.updated_at.desc(), GenerationJob.id.desc())
        rows = self.session.execute(
            select(GenerationJob, User.username, stuck.label("is_stuck"))
            .join(User, User.id == GenerationJob.owner_id)
            .where(*conditions)
            .order_by(*order)
            .limit(limit)
            .offset(offset)
        ).all()
        return [
            AdminJob(job=row[0], owner_name=row[1] or "", is_stuck=bool(row[2]))
            for row in rows
        ]

    def admin_count(
        self,
        *,
        statuses: tuple[str, ...] | None = None,
        username: str | None = None,
        kind: str | None = None,
        stuck_only: bool = False,
    ) -> int:
        """与 `admin_list` 同口径的命中总数（分页 / 「还有多少」用）。"""
        conditions: list[Any] = []
        if statuses:
            conditions.append(GenerationJob.status.in_(statuses))
        if kind:
            conditions.append(GenerationJob.kind == kind)
        if username:
            conditions.append(User.username.like(f"%{username}%"))
        if stuck_only:
            conditions.append(stuck_condition(now_text=utc_now_text()))
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
            select(
                GenerationJob,
                User.username,
                stuck_condition(now_text=utc_now_text()).label("is_stuck"),
            )
            .join(User, User.id == GenerationJob.owner_id)
            .where(GenerationJob.id == job_id)
        ).first()
        if row is None:
            return None
        return AdminJob(job=row[0], owner_name=row[1] or "", is_stuck=bool(row[2]))
