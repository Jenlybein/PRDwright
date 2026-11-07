"""任务运行时的内存形状与进程内登记表（`job_service` 的拆分子模块）。

从 `job_service` **机械迁出**（issue #5：单文件压到阈值以下，只搬代码不改行为）：
`JobRuntime` 的字段定义、`_SectionLive`，以及「进程内运行时表」`_RUNTIMES` 与
「保活集合」`_TASKS`。字段与注释逐字未改；#40 删空再导出块后，`JobRuntime` /
`_RUNTIMES` / `_TASKS` 由同一内部包按包内互引直接使用，`_SectionLive` 这类
名字也只在本包内使用，都不再经 `job_service` 对外再导出。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from backend.services.llm_meter import RunMeter

# 进程内的任务运行时：`job_id -> JobRuntime`
_RUNTIMES: dict[int, "JobRuntime"] = {}
# 保活集合：持有 `asyncio.Task` 强引用，防止未来得及跑完就被垃圾回收
_TASKS: set[asyncio.Task] = set()


@dataclass
class _SectionLive:
    """某一章「正在写」的实时缓冲（未定稿的原始增量）。

    只在分章节链路里出现：多章同时吐字，必须各存各的——混在一起就再也分不开，
    前端也就没法按章渲染（只能等整章定稿）。定稿（`part`）到达时整格丢弃，
    权威文本由 `parts_by_index` 接管。
    """

    section_id: str = ""
    text: str = ""
    # 该章当前所处阶段（writing / reviewing / rewriting）：快照用它还原卡片车道
    phase: str = "writing"


@dataclass
class JobRuntime:
    """一次生成任务在内存里的运行态（权威正文就在这里）。"""

    job_id: int
    owner_id: int
    solution_id: int | None
    kind: str
    status: str = "running"
    phase: str = ""
    # 已定稿段落（文档头 + 各章 part.text），按序拼接即「已产出正文」
    finalized_parts: list[str] = field(default_factory=list)
    # 并行分章节：按 index 存放的定稿段（各章 0..n）。与 finalized_parts 分工：
    # 文档头仍进 finalized_parts（它没有 index），章节进这里，`_draft` 按 index 排序装配。
    parts_by_index: dict[int, str] = field(default_factory=dict)
    # 是否有章节失败后被占位（仅 PRD 并行链路）
    partial: bool = False
    failed_sections: list[dict[str, Any]] = field(default_factory=list)
    # 当前段的实时缓冲（chunk 原始增量），遇到下一个 part 时清空。
    # 无章节归属的增量（整篇生成的文档、对话）落这里。
    live_buffer: str = ""
    # 分章节并行时的**按章**实时缓冲：`index -> 该章已吐出的原始增量`。
    #
    # 为什么要按章分：PRD 是多章同时生成的，几章的字混在一个 `live_buffer` 里
    # 就再也分不开了——那正是「PRD 只能等整章定稿才蹦出来」的历史成因。
    # 有了这一格，前端刷新重连时能按章把半成品摆回去、并接着往下长。
    #
    # 与 `live_buffer` 的分工：带 `section_id` 的 chunk 进这里，其余进 `live_buffer`。
    # 它**不落库**（过程信息，同 `stage_text`）：F5 不会重启后端进程，内存快照够用。
    live_by_index: dict[int, "_SectionLive"] = field(default_factory=dict)
    # 当前阶段模型的原始输出（审核意见 / 改写稿），只给阶段条下方的面板看。
    # 与 `live_buffer` 分道：后者是**产物正文**，混进来会把审核 JSON 写进文档预览。
    # 从前端内存独占这份文本，刷新就丢；改为运行时持有后，重连的 `snapshot` 能把它带回来。
    stage_text: str = ""
    # 已定稿章节的结构化记录（供快照恢复时定位「写到第几章」）
    completed_sections: list[dict[str, Any]] = field(default_factory=list)
    # 请求参数：派发时是单②摊平后的原样一份，`run_job` 开流前会把**服务端解析出的
    # 上游输入**合进来（见 `job_inputs`）。`_build_event_stream` / `_build_derived_from`
    # 都读这一份；落库的 `request_params` 投影不受影响。
    request: dict[str, Any] = field(default_factory=dict)
    # 本任务的**配置快照**（派发那一刻取的）。`run_job` 用 `use_config` 把它冻进上下文，
    # 于是整条链路（含并行子任务）读到的是同一份：既满足「改了下次生成生效」，
    # 又保证「跑到一半不会参数漂移」——后者会让章节乱序、正文重复，且极难归因。
    config: Any = None
    task: asyncio.Task | None = None
    failed: bool = False
    error_code: str = ""
    error_message: str = ""
    # 上次 flush 的时间与当时 live_buffer 的长度（用于「≤300ms 或增量 >4KB」判断）
    last_flush_at: float = 0.0
    flushed_len: int = 0
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
    # 是否已被取消（方案被删除时置位）。取消后事件循环不再消费后续事件，
    # 收尾也**不写**文档——见 `delete_solution_and_cancel_jobs` / `_finalize`。
    cancelled: bool = False
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
