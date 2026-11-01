"""对话与澄清领域的契约：会话创建 / 接续、工具结果回灌与对话轮次。

从 `backend.api.schemas` 拆出（#7）；对外引用由包的 `__init__` 再导出。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ============================================================================
# 会话（conversation）
# ============================================================================


class ConversationCreateRequest(BaseModel):
    """创建会话请求。

    不接受任何提示词参数：人设与首轮开场白由后端的对话档案决定
    （见 `services/prompts.py`），前端只能**点名场景**，不能自带提示词。
    """

    title: str = Field(default="", description="会话标题，留空则由服务层自动生成")
    answers: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "提交上来的数据。`clarification` 档案用问卷答案（key 为 QuestionConfig.name）；"
            "`doc-clarification` 档案用 document（文档正文）与 pending_items（待确认清单文本）"
        ),
    )
    profile_key: str = Field(
        default="",
        description=(
            "对话档案标识（如 `doc-clarification`）；留空取默认档案“需求澄清”。"
            "只认注册表里登记过的名字，未知取值直接 400——不静默回退，"
            "否则「点名了 A 场景、实际跑的是 B 场景」既没报错也没日志"
        ),
    )


class ClarificationAnswerItem(BaseModel):
    """结构化选择题里**一道题的作答**（工具结果回灌用）。

    `question` / `texts` 是冗余的自解释字段：工具结果要能独立看懂（对齐
    Claude Code `AskUserQuestion` 的结果形状），不能只给 `A` 这种标号——
    模型还得回头翻自己的工具调用参数才知道 `A` 是什么。
    """

    ask_id: str = Field(description="题目 id（后端分配的 ask-1 / ask-2）")
    labels: list[str] = Field(default_factory=list, description="选中项标号，如 [\"A\", \"B\"]")
    question: str = Field(default="", description="题干（自解释用）")
    texts: list[str] = Field(default_factory=list, description="选中项的选项文本")


class ToolResultPayload(BaseModel):
    """作答回灌的结构化载荷：一次「回答选择题」对应一条工具结果。"""

    tool_call_id: str = Field(description="要回答的工具调用 id（随 done 下发）")
    answers: list[ClarificationAnswerItem] = Field(
        default_factory=list, description="逐题作答；允许只答一部分"
    )


class MessageItem(BaseModel):
    """单条聊天消息，用于前端回传历史（无状态接续对话）。

    带 `tool_calls` / `tool_call_id` / `tool_result` 的消息是**工具调用链**的一环：
    助手提问走 `tool_calls`，用户作答走 `tool` 角色（`tool_result` 带结构化答案）。
    三者都可缺省——普通对话消息不需要它们。
    """

    role: Literal["system", "user", "assistant", "tool"] = Field(description="消息角色")
    content: str = Field(default="", description="消息正文")
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list,
        description="assistant 消息携带的工具调用，形如 [{id, name, args}]",
    )
    tool_call_id: str = Field(default="", description="tool 消息应答的工具调用 id")
    tool_result: ToolResultPayload | None = Field(
        default=None, description="tool 消息携带的结构化答案（有它时正文由它重新组装）"
    )


class ConversationContinueRequest(BaseModel):
    """接续对话请求。会话 ID 与历史至少提供一种。

    同样不接受提示词参数：人设由会话记录里的档案标识实时解析，前端不需要（也无法）
    重新下发。`profile_key` 也不是提示词，只是「点名场景」——服务重启后会话记录已丢失
    （无状态模式），没有它就只能回退默认档案，于是「文档澄清」会在中途悄悄变回「需求澄清」。

    `tool_result` 是结构化选择题的**作答回灌**：带上它时本轮不再投喂普通用户消息，
    改为向提问那次工具调用回一条 `ToolMessage`（对齐 Claude Code / Codex 的做法）。
    `content` 仍保留——它是给人看的展示文本（前端气泡）。
    """

    conversation_id: str = Field(
        default="",
        description="会话 ID；留空则由前端回传 history 走无状态模式",
    )
    content: str = Field(min_length=1, description="本轮用户输入的消息内容")
    history: list[MessageItem] = Field(
        default_factory=list,
        description="前端回传的历史消息；传入时优先于服务端保存的历史",
    )
    profile_key: str = Field(
        default="",
        description=(
            "会话记录缺失（服务重启 / 无状态模式）时用于解析人设的档案标识；"
            "有会话记录时以记录里的档案为准"
        ),
    )
    tool_result: ToolResultPayload | None = Field(
        default=None,
        description=(
            "结构化选择题的作答回灌。带上时本轮按工具结果回灌（ToolMessage），"
            "不重复投喂普通用户消息；`content` 仍保留作为展示文本"
        ),
    )

# ============================================================================
# 结构化摘要回填（对话 → 摘要）
# ============================================================================


class SummarySyncRequest(BaseModel):
    """摘要回填请求。"""

    summary: dict[str, Any] = Field(
        description="当前结构化需求摘要（与 field-schema.json 对齐）",
    )
    history: list[MessageItem] = Field(
        default_factory=list,
        description="用于回填的对话历史；为空时摘要原样返回",
    )


class SummarySyncResponse(BaseModel):
    """摘要回填响应。

    返回的是**完整**摘要（含所有未改动的字段），前端据此与本地摘要做 Diff 后
    弹面板给用户确认；`changed` 只是便利标志，判定仍以 Diff 结果为准。
    """

    summary: dict[str, Any] = Field(description="更新后的完整结构化需求摘要")
    changed: bool = Field(default=False, description="是否存在任何字段变化")


# ----------------------------------------------------------------------------
# 对话轮次（展示形状）：正文单独走 `GET /api/solutions/{id}/turns`，不塞进方案详情
# ----------------------------------------------------------------------------


class ChatTurn(BaseModel):
    """一条对话轮次（**展示形状**）。

    `role` 只会是 `user` / `assistant` / `system`：存储里的 `tool`（结构化选择题的
    作答，上游协议要求以工具结果回灌）在服务端就被投影成用户气泡。
    前端类型 `ChatMessage.role` 刻意不含 `tool`——让它流过去，会在复建时被
    白名单整条丢掉，表现为「刷新一次消息就没了」，而且不报错。
    """

    id: int = Field(description="轮次 id；同时是排序依据")
    role: str = Field(description="user / assistant / system")
    content: str = Field(default="", description="正文")
    stage: str = Field(default="", description="属于哪条线程")
    asks: list[dict[str, Any]] = Field(
        default_factory=list, description="这一轮附带的结构化选择题；没有则为空数组"
    )
    answers: list[dict[str, Any]] = Field(
        default_factory=list, description="作答回执（界面上这条是用户选的）"
    )
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list, description="这一轮发起的工具调用；回传上下文时不能裁"
    )
    interrupted: bool = Field(
        default=False, description="是否被用户中途停止（正文保留已生成的部分）"
    )
    created_at: str = Field(default="", description="写入时间（UTC ISO 8601）")


class ChatTurnListResponse(BaseModel):
    """某条线程的轮次列表，按发生顺序排列。"""

    turns: list[ChatTurn] = Field(default_factory=list, description="按 id 升序")
