"""跨层共享的数据契约。

这里存放 services 与 api 共享的数据契约，放在 core 是为了避免 services
反向依赖 api：`services/` 只应依赖 `core/`，不应回头 import `api/`。
`api/schemas.py` 从这里 re-export 这些模型，因此 API 层对外的引用
（`from backend.api.schemas import StreamEvent / QuestionsConfig`）保持不变。

本模块只允许依赖 `pydantic` / `typing`，**不得 import `backend.api.*`**——
一旦出现这种 import，就说明契约没沉到位，分层又被打破了。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ============================================================================
# 文档（documents）：三份产物正文在后端的一等实体
# ============================================================================

# 一份方案下的产物类型。注意与前端 `DocKind` 的差别：这是**产物**类型，不含
# `optimize`（那是动作、不是产物），且用下划线命名与后端 job kind 对齐。
DocumentKind = Literal["prd", "api_docs", "prompts"]

# 取值清单。`Literal` 只有类型、运行时取不到成员，需要遍历（校验、迁移映射）时用它。
DOCUMENT_KINDS: tuple[DocumentKind, ...] = ("prd", "api_docs", "prompts")


# ============================================================================
# 问题配置（对应 core/question_config.json）
# ============================================================================


class QuestionConfig(BaseModel):
    """问卷中的单个问题，字段与前端 `types/index.ts` 的 `QuestionConfig` 保持一致。"""

    name: str = Field(description="字段名称，作为提交答案时字典的 key")
    label: str = Field(description="展示给用户的题目文案")
    type: Literal["text", "textarea", "select", "radio"] = Field(
        description="题型：单行文本 / 多行文本 / 下拉选择 / 单选"
    )
    required: bool = Field(default=False, description="是否必填")
    options: list[str] = Field(default_factory=list, description="选择类题型的选项文案列表")


class QuestionsConfig(BaseModel):
    """问卷配置整体结构，按基础题 / 进阶题分组。"""

    base_questions: list[QuestionConfig] = Field(description="基础问题，创建会话时必答")
    advanced_questions: list[QuestionConfig] = Field(description="进阶问题，可按需补充")


# ============================================================================
# SSE 流式响应
# ============================================================================


class StreamEvent(BaseModel):
    """SSE 推送的单个事件。

    事件类型与 SSE 的 `event:` 行一一对应，前端既可以用
    `addEventListener('<type>')` 监听，也可以只解析 `data` 里的 `type` 字段。
    """

    type: Literal[
        "status",
        "chunk",
        "part",
        "done",
        "error",
        "stage",
        "tool_call",
        "document",
        "snapshot",
        "run_summary",
        "review",
        "stage_chunk",
    ] = Field(
        description=(
            "事件类型：状态提示 status / 增量文本 chunk / 定稿正文 part / 阶段进度 stage / "
            "结束 done / 错误 error，tool_call 与 document 为后续扩展预留。"
            "`stage` 用于分章节生成：`data.stage` 取 writing / reviewing / rewriting / done，"
            "`data.section_id` 标识当前章节（前端据此重置本章的实时缓冲）。"
            "`chunk` 在分章节链路里**必须带 `data.section_id` + `data.index`**：多章同时"
            "吐字，没有归属前端只能整批丢弃（那正是「PRD 不流式」的成因）；它是模型的"
            "原始输出，只负责「让用户现在就看见它在长」，权威正文仍以 `part` 为准。"
            "`part` 也用于分章节生成：每段正文（文档头、每一章）一被定稿就整段下发，"
            "`data.text` 可直接追加到文档末尾，前端据此拼出与最终 `done.reply` 逐字一致的预览。"
            "`snapshot` 与 `run_summary` 用于生成任务（generation_jobs）：前者在每次订阅开始时 "
            "**首发给**，`data` 含 job_id/kind/status/phase/finalized_text/live_buffer/"
            "live_by_section/stage_text/completed_sections/solution_id/error_code/"
            "error_message/elapsed_ms，供前端补全已产出正文与当前阶段输出"
            "（`live_by_section` 是按章的半成品，只有分章节链路非空，不落库）；"
            "后者在任务收尾时下发本次生成的用量汇总（结构见 `RunSummary`）。"
            "`review` 是**不持久化**的审核结论：`data` 含 kind/verdict/summary/issues/"
            "rewritten/failed_sections，仅在本次订阅内展示，刷新即失。"
            "`stage_chunk` 是**阶段内的原始输出增量**（审核意见 / 改写稿正文），"
            "`data` 含 stage（reviewing / rewriting）与 doc_type；"
            "它与 `chunk` 是两条独立通道——`chunk` 进文档预览，它只进阶段输出面板，"
            "混用会把审核意见写进正文。"
        )
    )
    content: str = Field(default="", description="文本内容（增量或提示语）")
    data: dict[str, Any] = Field(default_factory=dict, description="事件附加数据")
    code: str | None = Field(
        default=None,
        description=(
            "机器可读的错误码（`backend/core/errors.py` 的 `ErrorCode` 成员），"
            "供前端做分支判断。只有 `error` 事件会带，其余事件恒为 `None`。"
            "刻意放在**顶层**而不是塞进 `data`：SSE 流是 HTTP 200，前端拿不到状态码，"
            "判断「该不该降级重试」只能靠这个字段，放在顶层才不会被 `data` 的"
            "其它附加字段淹没。缺失或取值未知时前端一律按 `INTERNAL_ERROR` 处理。"
        ),
    )


# ============================================================================
# 本次生成的用量汇总
# ============================================================================


class StepUsage(BaseModel):
    """一个生成步骤的用量累计（如 PRD 的撰写 / 审核 / 改写各算一步）。

    步骤名由「任务类型 + 阶段」推出（`prd:writing` / `api_docs` …），见
    `services/llm_meter.py`：模型调用发生在流水线内部，那里拿不到调用方的语义名，
    却能从 job 的 kind 与已在流转的 `stage` 事件里唯一确定自己在哪一步。
    """

    step: str = Field(description="步骤名，形如 `prd:writing`")
    calls: int = Field(default=0, description="该步骤内**拿到结果**的模型调用次数")
    failed_calls: int = Field(
        default=0,
        description="该步骤内失败的调用次数（超时、断连、5xx，含重试前的废弃尝试）",
    )
    prompt_tokens: int = Field(default=0, description="输入 token 累计")
    completion_tokens: int = Field(default=0, description="输出 token 累计")
    duration_ms: int = Field(default=0, description="该步骤累计耗时（毫秒）")


class SkillRef(BaseModel):
    """一次生成里实际注入的技能包身份。

    用来回答「这份 PRD 是按哪一套模板写的」：改了 `references/prd-template.md` 之后指纹
    就会变，两份文档的生成条件能不能对得上，回执上直接看得到，不必去翻当时的文件。

    列表为空（且这次是 PRD 任务）意味着技能包没加载上、走的是内置兜底提示词——
    这不是一个应当静默发生的情况（债务 D8），前端会把它显示成一条告警。
    """

    name: str = Field(default="", description="技能包目录名，如 prd-generator")
    version: str = Field(default="", description="skill.yaml 声明的版本号；读不到时为 unknown")
    fingerprint: str = Field(default="", description="三份资源内容合成的整体指纹（前 12 位）")


class RunSummary(BaseModel):
    """一次生成任务的用量汇总：`run_summary` 事件与 `generation_jobs.usage` 共用同一结构。

    字段名与前端 `types/index.ts::RunSummary` **逐字一致**（前端用 camelCase 归一化），
    契约准绳在前端，改任何一侧都要同步另一侧。
    """

    kind: str = Field(default="", description="任务类型：prd / api_docs / prompts / optimize")
    status: str = Field(default="", description="终态：completed / failed")
    duration_ms: int = Field(default=0, description="任务总耗时（毫秒）")
    total_calls: int = Field(default=0, description="模型调用总次数（只统计拿到结果的调用）")
    failed_calls: int = Field(
        default=0,
        description=(
            "失败的调用次数。与 `total_calls` 分开是为了不互相污染：两者都是真实发生的请求，"
            "混在一起会让「总共发了几次」失真。它的耗时也已计入各 step 的 `duration_ms`。"
        ),
    )
    prompt_tokens: int = Field(default=0, description="输入 token 总量")
    completion_tokens: int = Field(default=0, description="输出 token 总量")
    steps: list[StepUsage] = Field(default_factory=list, description="按步骤拆分的用量")
    injected_skills: list[SkillRef] = Field(
        default_factory=list,
        description="本次注入的技能规范文件。只有 PRD 生成会这项不为空；"
        "其他任务类型恒为空列表，不代表降级。",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "事前预算的告警（如「某步的输入已达预算上限的 92%」），每个 step 至多一条、"
            "保留最严重的那条。文案由后端生成、仅供展示：前端**不做**分支判断，"
            "也更不该拿它做重试决策——真正的拦截在超顶时已经转成 `error` 事件了。"
        ),
    )
    protocol: str = Field(
        default="",
        description="实际使用的**协议**（ark / openai_compatible / anthropic …）。"
        "它是「用哪套适配器、查哪组限值」的判据，也是账单按供应商聚合的口径。"
        "⚠️ 不是配置名、也不是配置 id。",
    )
    provider_config_id: str = Field(
        default="",
        description="实际使用的**配置条目** id：`cfg_xxx`；若来自环境变量合成，则是 "
        "`env:ark` 这种带前缀的伪 id（表示这次没用库里的配置）。排障时按它可以定位到具体一条。",
    )
    provider_name: str = Field(
        default="",
        description="配置名的**快照**（如「方舟-生产」）。存快照不存指针：配置可以被改名、"
        "被删除，历史回执仍要能解释「当时用的是哪一条」。",
    )
    model: str = Field(default="", description="最终使用的模型名")
    request_id: str = Field(default="", description="该次生成所属请求的追踪 id")
    partial: bool = Field(
        default=False,
        description="本次生成是否有章节失败后被占位（仅 PRD 分章节；其余任务恒为 false）。"
        "true 表示正文可用但不完整，前端据此在审阅页给出提示。",
    )
    failed_sections: list[dict[str, Any]] = Field(
        default_factory=list,
        description="失败章节清单（section_id / title / index / reason）。"
        "与 partial 配套：前者是结论，这里是可定位的细节。",
    )
