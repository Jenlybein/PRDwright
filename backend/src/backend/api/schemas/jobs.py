"""生成任务（generation_jobs）：把一次生成持久化并支持刷新重连。

从 `backend.api.schemas` 拆出（#7）；对外引用由包的 `__init__` 再导出。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.core.contracts import DocumentKind, RunSummary

from .conversation import MessageItem

# ============================================================================
# 生成任务（generation_jobs）：把一次生成持久化并支持刷新重连
# ============================================================================

# 任务类型。前三种是「整篇生成」，与前端 `DocumentKind` 一一对应；
# `optimize`（按用户指令就地优化）、`clarify`（按澄清对话就地改写）、
# `auto_clarify`（AI 自行拍板待确认项后就地改写）与 `review`（只审不改、重审当前正文）
# 都是**对某一份已有文档的动作**，因此它们必须额外带 `doc_kind` 才能落库。
#
# `review` 与生成链路共用同一段审核代码，区别只在收尾：它**不写正文**，
# 只把结论写进被审的那一版（ADR 0002 第 2 条）。
#
# `chat` 是**对话的一轮**：它不写文档，产出是一条 `chat_turns` 轮次。
# 把对话塞进同一条任务通道，换来的是「切走不断」「刷新接回」「换设备能看」
# 三者共用现有机制——它们全建立在 `jobId + JobBus + draft_content` 上，
# 对话原本一样都没有。
JobKind = Literal[
    "prd", "api_docs", "prompts", "optimize", "clarify", "auto_clarify", "review", "chat"
]


class JobCreateRequest(BaseModel):
    """创建一次生成任务的请求体。

    一个模型承载五种任务：必填字段按 `kind` 区分（路由层校验），
    因此不同 `kind` 只用到各自相关的字段，其余留默认值即可。
    """

    kind: JobKind = Field(
        description="任务类型：prd / api_docs / prompts / optimize / clarify / auto_clarify / review"
    )
    solution_id: int | None = Field(
        default=None,
        description="关联方案 id；快捷入口等场景可为空（逃生口）",
    )
    doc_kind: DocumentKind | None = Field(
        default=None,
        description=(
            "本次任务**作用在**哪一份产物上。`optimize` / `clarify` / `auto_clarify` /"
            "`review` 必须给：它们的 `kind` 表达的是「做了一件什么事」，而落库要知道"
            "「东西写进（或读到）哪一份文档」。缺省时 `review` 直接 400，其余不落库"
            "（宁可本次改动只留在会话里，也不猜一个默认值写错位置）。"
        ),
    )
    # --- prd ---
    summary: dict[str, Any] | None = Field(
        default=None,
        description="结构化需求摘要（完整流程）；与 requirements 二选一",
    )
    requirements: str = Field(default="", description="自由文本需求（PRD 快捷入口）")
    product_name: str = Field(default="", description="产品名称，仅用于 PRD 文档头")
    history: list[MessageItem] = Field(
        default_factory=list,
        description="补充对话记录；为空则按无对话处理",
    )
    # --- api_docs / prompts ---
    prd_content: str = Field(default="", description="PRD 正文（接口文档 / 提示词生成输入）")
    api_docs_content: str = Field(
        default="",
        description=(
            "接口文档正文（仅 `prompts` 可选）。给「我有 PRD + 接口文档」入口用：这两份材料都在"
            "手上时，提示词可以同时参考它们。留空表示没有这份材料，生成按「只有 PRD」处理。"
        ),
    )
    # --- optimize / clarify ---
    document: str = Field(default="", description="待优化的原文档正文")
    instruction: str = Field(default="", description="用户的优化要求（仅 optimize）")
    # --- auto_clarify ---
    pending_items: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "要由 AI 自行拍板的待确认清单（仅 `auto_clarify`）。传结构化项而不是拼好的文本："
            "提示词由后端持有，前端只能给数据。"
        ),
    )
    # --- chat ---
    stage: str = Field(
        default="",
        description=(
            "对话线程（仅 `chat`，必填）：requirement / prd-clarify / "
            "api-docs-clarify / prompts-clarify。**缺省不猜默认值**——猜错等于把 A 线程"
            "的对话记到 B 线程头上，而四条线程的人设与上下文完全不同。"
        ),
    )
    content: str = Field(default="", description="本轮用户输入（仅 `chat`）")
    chat_mode: str = Field(
        default="continue",
        description=(
            "这一轮是**开新会话**还是**续聊**（仅 `chat`）：`start` 会用 `form_data` "
            "渲染开场白并新建一条会话；`continue` 走续聊。**不给默认值的语义留白**——"
            "缺省取 `continue` 是因为续聊是绝大多数情况，而漏传 `start` 的后果是"
            "「开场白没生成、模型直接对着一句空话作答」，用户一眼能看出来。"
        ),
    )
    form_data: dict[str, Any] | None = Field(
        default=None,
        description=(
            "开新会话要提交的表单（仅 `chat` 且 `chat_mode=start`）："
            "需求澄清传问卷答案；文档澄清传 `{document, pending_items}`。"
        ),
    )
    tool_result: dict[str, Any] | None = Field(
        default=None,
        description=(
            "结构化选择题的作答（仅 `chat`）：`{tool_call_id, answers}`。"
            "带上时按工具结果回灌给当初那次工具调用，不再重复投喂人话。"
        ),
    )
    profile_key: str = Field(
        default="",
        description=(
            "对话档案标识（仅 `chat`）：后端据此选人设与开场白；留空取默认档案。"
            "命中不了注册表时**直接 400**，不静默退回默认人设——否则文档澄清会在中途"
            "悄悄变回需求澄清，用户突然被问「要不要生成 PRD」却没有任何报错。"
        ),
    )
    # --- prd / api_docs / prompts ---
    review_enabled: bool | None = Field(
        default=None,
        description=(
            "本次生成是否带复核（仅 `prd` / `api_docs` / `prompts` 三种整篇生成）："
            "`prd` 是逐章审核 + 改写，`api_docs` / `prompts` 是整篇审一次 + 必要时改写一次。"
            "`null`（默认）取服务配置（后两者默认为开）；快捷流程传 `false`——输入只有一段话，"
            "复核的边际收益低，而「只撰写、不审不改」能省下约一半调用。"
        ),
    )


class JobCreated(BaseModel):
    """创建任务响应：返回任务 id 与初始状态。"""

    job_id: int = Field(description="任务 id")
    status: str = Field(default="running", description="任务状态，创建时恒为 running")


class JobStatus(BaseModel):
    """任务状态摘要。**刻意不含 `draft_content`**：正文只走 stream 的 snapshot。

    `id` 用 `job_id` 作为对外字段名，与创建响应保持一致；内部主键列名是 `id`。
    """

    id: int = Field(description="任务 id")
    kind: str = Field(description="任务类型")
    doc_kind: str | None = Field(
        default=None,
        description=(
            "目标产物（prd / api_docs / prompts）。`optimize` / `clarify` 这类动作凭 `kind` "
            "定不了产物，靠这个字段告诉前端「改的是哪一份」；取不到时为 null，"
            "调用方不得猜一个默认值。"
        ),
    )
    chat_stage: str | None = Field(
        default=None,
        description=(
            "对话任务属于哪条线程（仅 `chat`）。`kind` 只说「这是对话」，四条线程的"
            "人设与上下文完全不同——刷新后要认领这一轮，就得知道它是哪条线程的。"
            "非对话任务为 null。"
        ),
    )
    status: str = Field(description="任务状态：running / completed / failed")
    phase: str = Field(default="", description="当前阶段（如 writing / reviewing）")
    solution_id: int | None = Field(default=None, description="关联方案 id，可空")
    error_code: str = Field(default="", description="失败时的错误码")
    error_message: str = Field(default="", description="失败时的中文说明")
    created_at: str = Field(description="创建时间（UTC ISO 8601）")
    updated_at: str = Field(description="最近更新时间（UTC ISO 8601）")
    partial: bool = Field(
        default=False,
        description="本次生成是否有章节失败后被占位（仅 PRD 并行链路）。"
        "true 表示正文可用但不完整，前端据此在审阅页提示。",
    )
    usage: RunSummary | None = Field(
        default=None,
        description=(
            "本次生成的用量汇总（token / 耗时 / 步骤 / 模型）。"
            "与 SSE 的 `run_summary` 事件**同结构、同来源**；任务尚未收尾或记录损坏时为 null。"
        ),
    )


class JobListResponse(BaseModel):
    """任务列表响应。

    包一层对象而不是直接返回数组：将来要加分页或总数时不必改响应结构，
    与 `SolutionListResponse` 保持同一约定。
    """

    jobs: list[JobStatus] = Field(default_factory=list, description="当前用户的任务列表")
