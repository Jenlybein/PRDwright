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

两条**只留在本 docstring、不再有常量承载**的口径（原本的 `DEFAULT_CHAT_STAGE` /
`TURN_ROLES` 两个常量在 #3 清理中删掉了，因为已经没有代码读它们）：

- **旧快照的落点**：取不到线程标识时，旧快照（`messages` 还没有 `stage` 字段的那个年代）
  一律按需求澄清处理；但**新请求缺 `stage` 应当报 400**，不要静默落到需求澄清。
- **`tool` 是存储形状，不是展示形状**：结构化选择题的作答，在上游 API 里必须以
  `ToolMessage` 回给当初发起调用的 assistant，否则历史里留下「调了工具却没人应答」，
  上游直接报错。前端从不接收这个角色（`ChatMessage.role` 刻意不含它），
  读接口会把 `tool` 投影成用户气泡。
"""

from __future__ import annotations

# 四条澄清线程。顺序即前端标签的展示顺序，不要随意调整。
CHAT_STAGES: tuple[str, ...] = (
    "requirement",
    "prd-clarify",
    "api-docs-clarify",
    "prompts-clarify",
)

# 取不到线程标识时的落点（旧快照默认属需求澄清）与轮次角色的存储口径，见本模块 docstring。
# 这里刻意不再放 `DEFAULT_CHAT_STAGE` / `TURN_ROLES` 两个常量：它们在全仓已无读者，
# 而它们承载的知识是「口径」而不是「取值」，留在 docstring 里比留一个没人读的元组更可靠。


def is_valid_stage(value: object) -> bool:
    """是否为合法的线程标识（写入路径用它做准入判断）。"""
    return str(value or "") in CHAT_STAGES
