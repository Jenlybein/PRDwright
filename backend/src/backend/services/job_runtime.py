"""任务运行时的内存形状与进程内登记表（`job_service` 的拆分子模块）。

从 `job_service` **机械迁出**（issue #5：单文件压到阈值以下，只搬代码不改行为）：
`JobRuntime` 的字段定义，以及「进程内运行时表」`_RUNTIMES` 与「保活集合」`_TASKS`。
`_RUNTIMES` / `_TASKS` 由同一内部包按包内互引直接使用，都不再经 `job_service` 对外再导出。

票 #92 起 PRD 与另两份产物共用同一条**整篇**生成路径，章节级运行时状态
（`_SectionLive` / `live_by_index` / `parts_by_index` / `completed_sections`）全部退役：
正文只有「已定稿段 + 一段实时缓冲」两种形态。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from backend.core.contracts import StreamEvent
from backend.services.llm_meter import RunMeter

# 进程内的任务运行时：`job_id -> JobRuntime`
_RUNTIMES: dict[int, "JobRuntime"] = {}
# 保活集合：持有 `asyncio.Task` 强引用，防止未来得及跑完就被垃圾回收
_TASKS: set[asyncio.Task] = set()


@dataclass
class JobRuntime:
    """一次生成任务在内存里的运行态（权威正文就在这里）。"""

    job_id: int
    owner_id: int
    solution_id: int | None
    kind: str
    status: str = "running"
    phase: str = ""
    # 已定稿段落。整篇生成只有一个 `part{replace}`（审核发现问题后的那次改写），
    # 按序拼接即「已产出正文」。
    finalized_parts: list[str] = field(default_factory=list)
    # 当前段的实时缓冲（chunk 原始增量），遇到下一个 part 时清空。
    live_buffer: str = ""
    # 「有内容可用但不完整」（#85）：由 `done.partial` 带来，随终态同一条语句落库。
    partial: bool = False
    # 生成失败的章节清单。分章链路已随票 #92 退役，三份产物都整篇生成，因此当前恒为空；
    # 保留字段是为了既有结论 / 回执形状不变（#94 统一收口时再决定去留）。
    failed_sections: list[dict[str, Any]] = field(default_factory=list)
    # 当前阶段模型的原始输出（审核意见 / 改写稿），只给阶段条下方的面板看。
    # 与 `live_buffer` 分道：后者是**产物正文**，混进来会把审核 JSON 写进文档预览。
    # 从前端内存独占这份文本，刷新就丢；改为运行时持有后，重连的 `snapshot` 能把它带回来。
    stage_text: str = ""
    # 请求参数：派发时是单②摊平后的一份，**服务端解析出的上游输入**在 `as_event_request`
    # 里已经合进来（见 `job_inputs`：解析发生在建任务那一刻）。`_build_event_stream` /
    # `_build_derived_from` 都读这一份；落库的 `request_params` 投影不受影响。
    request: dict[str, Any] = field(default_factory=dict)
    # 本任务的**配置快照**（派发那一刻取的）。`run_job` 用 `use_config` 把它冻进上下文，
    # 于是整条链路读到的是同一份：既满足「改了下次生成生效」，
    # 又保证「跑到一半不会参数漂移」。
    config: Any = None
    task: asyncio.Task | None = None
    failed: bool = False
    error_code: str = ""
    error_message: str = ""
    # 上次 flush 的时间与当时 live_buffer 的长度（用于「≤300ms 或增量 >4KB」判断）
    last_flush_at: float = 0.0
    flushed_len: int = 0
    # --- 过程日志（事件表，票 #86）---
    #
    # 已经应用进运行时、但还没落进 `job_events` 的事件。逐条到来（模型增量可能一秒
    # 几百条），按既有的合并写窗口一批落盘（与进度列同一笔事务），因此这里只是内存
    # 缓冲，不是第二份正文。
    #
    # ⚠️ 只在事务**提交之后**清空（`services.job_events.mark_flushed`）：回滚了还清，
    # 那一批就永久丢了，症状是「断线重连少了一段」，极难归因。
    pending_events: list[StreamEvent] = field(default_factory=list)
    # 已落盘的最后一条事件的游标。订阅者的快照游标就是它——「快照内容覆盖到哪一条」
    # 的判据，因此快照与事件必须同一笔提交（见 `job_runtime_events._maybe_flush`）。
    last_event_id: int = 0
    # 任务开始时刻（算 duration_ms）
    started_at: float = 0.0
    # done 事件带来的权威完整正文
    final_reply: str = ""
    # 本次任务的用量计量器（在 `dispatch` 里按任务类型与当前供应商构造）
    meter: RunMeter = field(default_factory=RunMeter)
    # 注入的技能包身份（PRD 任务才有），进 `run_summary.injected_skills`。
    # 在 dispatch 那一刻定格：万一生成中途改了模板，回执仍能说明「这次用的是哪一套」。
    skill_refs: list[dict[str, Any]] = field(default_factory=list)
    # 待确认清单：由 `done` 事件带来（生成器在定稿正文里剥离标记块后产出）。
    # 与正文同源、同一次收尾落库，因此「换了正文却忘了重算清单」没有结构落脚点。
    pending_items: list[dict[str, Any]] = field(default_factory=list)
    # 本次审核结论（来自 `review` 事件；形状与事件 data 一致）。
    #
    # 它**只在这里中转一次**：收尾时随正文一起写进被审的那一版 `document_versions`
    # （`review` 列），成为刷新 / 换设备后的唯一来源。SSE 事件本身不重放，
    # 所以「订阅晚了一步就看不到结论」不影响落库。
    #
    # `None` 表示没审成（或这次不审）：**绝不写一个「通过」占位**——没有结论
    # 不等于通过，界面据此标「审核失败」。
    review: dict[str, Any] | None = None
    # 本次任务**有没有真的试过**审核（由 `review` 事件上的 `attempted` 显式标记）。
    #
    # 它只回答一个问题：「没结论」是因为**审失败了**，还是因为**压根没审**？
    # 两者在库里长得一模一样（都只有空列），但含义相反：前者要落 `review_failed_at`
    # （刷新后仍显示「审核失败」），后者必须保持「未审核」（快捷流程关掉了审核）。
    # ⚠️ 不要用「结论为空」推它：空正文那条路同样没有结论，但那不是失败。
    review_attempted: bool = False
    # 是否已被取消（方案被删除时置位）。取消后事件循环不再消费后续事件，
    # 收尾也**不写**文档——见 `delete_solution_and_cancel_jobs` / `_finalize`。
    cancelled: bool = False
    # 取消请求的原因文案（「用户停止了生成」/「方案已删除」/「管理员清理卡死」…）。
    # 与 `error_message` 分开：取消不是失败，不该借失败原因那一列；确认取消时这一句
    # 就是最终落进 `error_message` 的原因（AC4：状态由 status 表达，原因由文案表达）。
    cancel_message: str = ""
    # --- 执行权（租约，票 #82）---
    #
    # 这一轮的执行权由三件事构成（判权看凭证、不看归属）：谁领的（仅供排查）、
    # 本轮凭证、到期时间。派发时从抢占结果里拷进来；收尾时把凭证递给状态机，
    # 由数据库判「我还有没有资格写这一行」；每次模型调用前续租。
    #
    # 没有租约的运行时（本票之前建的老行 / 测试直造的假单）三列都为空：那时判权退回
    # 「只看状态」，与 #79 / #81 的行为一致。
    lease_owner: str = ""
    lease_token: str = ""
    lease_expire_at: str = ""
    # 是否已被**同一份文档的新任务**顶替（`create_job` 在建新任务时置位）。
    #
    # 与 `cancelled` 分开：那个是「方案没了，谁也别写」，这个是「有更晚的结果要写，
    # 你先让开」。两者在收尾时都不写文档，但原因不同，`error_code` 也要能区分
    # （`CANCELLED` vs `SUPERSEDED`）——否则排查时会误以为是用户删了方案。
    #
    # ⚠️ 只落库是不够的：`supersede_active` 改的是**库里那一行**，内存里的旧运行时
    # 完全不知道自己被顶替了，于是它收尾时照样 `_write_document`，把旧稿盖在新稿上。
    # 用户看到的是「新任务跑完了，正文却是上一版的」——不报错，只是内容对不上。
    superseded: bool = False

    # --- 对话任务（`kind='chat'`）专有 ---
    #
    # 对话与文档的关键差别：**对话只追加、不覆盖**。因此它既不需要 `superseded`，
    # 也不能沿用「取消就不落库」的口径——用户按停止，已经说出口的话仍然算数。
    #
    # 这一轮属于哪条线程（`requirement` / `prd-clarify` / …）。四条线程的人设与
    # 上下文互不相通，没有它就分不清「这轮回复该记到谁头上」。
    chat_stage: str = ""
    # 本轮结束后要写的轮次 id（创建任务时已落库的那一条输入轮次）
    chat_input_turn_id: int = 0
    # 后端会话 id（`done` 事件带回）：只用于下一轮续聊，丢了会自动降级为无状态模式
    chat_conversation_id: str = ""
    # 这一轮的提问与它发起的工具调用，都来自 `done`
    chat_asks: list[dict[str, Any]] = field(default_factory=list)
    chat_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # 被用户中途停止：收尾时保留已生成的部分，并给轮次打上标记
    chat_interrupted: bool = False


def reset_for_tests() -> None:
    """清空进程内运行时表与保活集合（**测试夹具专用**）。

    生产代码没有调用点：正常收尾已经会把运行时 `pop` 掉（见 `run_job` 的 finally）。
    留一个公开口子是为了让用例不必去摸 `_RUNTIMES` / `_TASKS` 这两个私有名——与
    `core.engine.reset_engine` / `core.logging_setup.reset_logging` 同一套写法。
    """
    _RUNTIMES.clear()
    _TASKS.clear()
