"""澄清对话的共享词汇：线程标识与轮次角色。

为什么单独一个模块
------------------
这两组取值会同时出现在**三处**，任何一处各写各的字符串，就会得到「消息归错线程」
这种不报错、只表现为「模型答得前言不搭后语」的故障：

- 数据库：`chat_turns.stage` / `chat_sessions.stage`；
- 任务：`POST /api/jobs` 的 `stage` 字段（创建对话任务时必须给）；
- 前端：`types/index.ts` 的 `ChatStage`。

因此这里放**唯一**一份后端定义，并用 `tests/test_chat_contract.py` 扫描前端源码
断言两个集合相等——与错误码那套契约测试是同一个范式。加第五条线程时两处一起红，
而不是等用户发现某条线程的历史串到另一条上。

⚠️ 命名口径：前端用连字符（`api-docs-clarify`），这里也用连字符。历史原因，
`generation_jobs.kind` 那边的文档种类用下划线（`api_docs`），两套口径的转换收口在
`api/jobs.py`，不要在数据库里混用。
"""

from __future__ import annotations

# 四条澄清线程。顺序即前端标签的展示顺序，不要随意调整。
CHAT_STAGES: tuple[str, ...] = (
    "requirement",
    "prd-clarify",
    "api-docs-clarify",
    "prompts-clarify",
)

# 取不到线程标识时的落点：旧快照（`messages` 没有 `stage` 字段的那个年代）一律属需求澄清。
# **只用于迁移与兼容旧数据**，新请求缺 `stage` 时应当报 400，而不是静默落到这里。
DEFAULT_CHAT_STAGE = "requirement"

# 轮次角色。
#
# `tool` 是**存储形状**而不是展示形状：结构化选择题的作答，在上游 API 里必须以
# `ToolMessage` 回给当初发起调用的 assistant，否则历史里留下「调了工具却没人应答」，
# 上游直接报错。前端从不接收这个角色（`ChatMessage.role` 刻意不含它），读接口会把
# `tool` 投影成用户气泡。
TURN_ROLES: tuple[str, ...] = ("user", "assistant", "system", "tool")


def normalize_stage(value: object) -> str:
    """把任意来源的线程标识收敛成合法取值；认不出时退回需求澄清。

    仅用于**读旧数据**。新请求请用 `is_valid_stage` 显式校验并拒绝非法值——
    「猜一个默认值」在写入路径上等于把 A 线程的对话记到 B 线程头上。
    """
    stage = str(value or "")
    return stage if stage in CHAT_STAGES else DEFAULT_CHAT_STAGE


def is_valid_stage(value: object) -> bool:
    """是否为合法的线程标识（写入路径用它做准入判断）。"""
    return str(value or "") in CHAT_STAGES


def is_valid_role(value: object) -> bool:
    """是否为合法的轮次角色。"""
    return str(value or "") in TURN_ROLES
