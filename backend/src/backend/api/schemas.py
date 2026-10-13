"""API 请求 / 响应模型定义。

路由层只负责参数校验与序列化，业务数据结构统一在这里声明，
避免请求体 / 响应体的字段散落在各个路由文件里。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

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
# 会话（conversation）
# ============================================================================


class ConversationCreateRequest(BaseModel):
    """创建会话请求。"""

    title: str = Field(default="", description="会话标题，留空则由服务层自动生成")
    answers: dict[str, Any] = Field(
        default_factory=dict,
        description="问卷答案，key 为 QuestionConfig.name",
    )
    system_prompt_override: str = Field(
        default="",
        description=(
            "本轮会话专用的系统提示词覆盖。传入时用它替代服务层的默认系统提示词，"
            "例如前端为「需求澄清对话」下发的专用人设；留空则沿用默认。"
        ),
    )


class MessageItem(BaseModel):
    """单条聊天消息，用于前端回传历史（无状态接续对话）。"""

    role: Literal["system", "user", "assistant"] = Field(description="消息角色")
    content: str = Field(default="", description="消息正文")


class ConversationContinueRequest(BaseModel):
    """接续对话请求。会话 ID 与历史至少提供一种。"""

    conversation_id: str = Field(
        default="",
        description="会话 ID；留空则由前端回传 history 走无状态模式",
    )
    content: str = Field(min_length=1, description="本轮用户输入的消息内容")
    history: list[MessageItem] = Field(
        default_factory=list,
        description="前端回传的历史消息；传入时优先于服务端保存的历史",
    )
    system_prompt_override: str = Field(
        default="",
        description=(
            "系统提示词覆盖。有状态模式下会沿用创建会话时保存的人设，"
            "无状态模式（只传 history）下需由前端重新下发。"
        ),
    )


# ============================================================================
# SSE 流式响应
# ============================================================================


class StreamEvent(BaseModel):
    """SSE 推送的单个事件。

    事件类型与 SSE 的 `event:` 行一一对应，前端既可以用
    `addEventListener('<type>')` 监听，也可以只解析 `data` 里的 `type` 字段。
    """

    type: Literal[
        "status", "chunk", "part", "done", "error", "stage", "tool_call", "document"
    ] = Field(
        description=(
            "事件类型：状态提示 status / 增量文本 chunk / 定稿正文 part / 阶段进度 stage / "
            "结束 done / 错误 error，tool_call 与 document 为后续扩展预留。"
            "`stage` 用于分章节生成：`data.stage` 取 writing / reviewing / rewriting / done，"
            "`data.section_id` 标识当前章节（前端据此重置本章的实时缓冲）。"
            "`part` 也用于分章节生成：每段正文（文档头、每一章）一被定稿就整段下发，"
            "`data.text` 可直接追加到文档末尾，前端据此拼出与最终 `done.reply` 逐字一致的预览。"
        )
    )
    content: str = Field(default="", description="文本内容（增量或提示语）")
    data: dict[str, Any] = Field(default_factory=dict, description="事件附加数据")


# ============================================================================
# 接口文档检索增强（RAG）
# ============================================================================


class RagHit(BaseModel):
    """一条检索命中（团队接口规范 / 历史接口示例）。"""

    id: str = Field(default="", description="条目 ID")
    source: str = Field(default="", description="来源，如「团队接口规范」「历史接口示例」")
    title: str = Field(default="", description="条目标题")
    content: str = Field(description="条目正文（规范条文或历史接口示例）")
    tags: list[str] = Field(default_factory=list, description="标签，便于前端分组展示")
    score: float = Field(default=0.0, description="相似度得分，越大越相关")
    matched_terms: list[str] = Field(
        default_factory=list,
        description="命中的关键词，用于向用户解释「为什么检索到这条」",
    )


class ApiDocsRagRequest(BaseModel):
    """接口文档检索请求。"""

    prd_content: str = Field(min_length=1, description="PRD 正文（Markdown）")
    history: list[MessageItem] = Field(
        default_factory=list,
        description="对话历史，作为检索查询的补充",
    )
    limit: int = Field(default=5, ge=1, le=20, description="返回条数上限")


class ApiDocsRagResponse(BaseModel):
    """接口文档检索响应。"""

    hits: list[RagHit] = Field(default_factory=list, description="命中列表，按相关度降序")


# ============================================================================
# 文档流式生成（对应 document_service 的 *_stream 方法）
# ============================================================================


class GeneratePrdStreamRequest(BaseModel):
    """生成 PRD 的流式请求。"""

    requirements: str = Field(
        min_length=1,
        description="需求澄清结果（问卷答案整理成的文本）",
    )
    history: list[MessageItem] = Field(
        default_factory=list,
        description="补充对话记录；为空则按无对话处理",
    )


class GeneratePrdFromSummaryStreamRequest(BaseModel):
    """生成 PRD 的流式请求（需求输入二选一）。

    `summary` 与 `requirements` 都必须支持，因为入口不止一个：

    - **完整流程**传 `summary`——与 `field-schema.json` 对齐的结构化摘要；
    - **PRD 快捷入口**传 `requirements`——用户直接用自然语言写下的一段话。

    两者在服务层汇合到同一条技能包驱动链路（分章节撰写 → 审核 → 改写），
    因此「入口」只决定输入怎么来，不决定文档怎么写。
    """

    summary: dict[str, Any] | None = Field(
        default=None,
        description="结构化需求摘要（与 field-schema.json 对齐）；与 requirements 二选一",
    )
    requirements: str = Field(
        default="",
        description="自由文本需求（PRD 快捷入口）；仅在未传 summary 时使用",
    )
    product_name: str = Field(
        default="",
        description=(
            "产品名称，仅用于 PRD 文档头与 1.1 节；快捷入口传用户填写值。"
            "摘要里已有 product_name 时以摘要为准"
        ),
    )
    history: list[MessageItem] = Field(
        default_factory=list,
        description="补充对话记录；为空则按无对话处理",
    )


class GenerateApiDocsStreamRequest(BaseModel):
    """生成接口文档的流式请求。"""

    prd_content: str = Field(min_length=1, description="PRD 正文（Markdown）")
    rag_hits: list[RagHit] = Field(
        default_factory=list,
        description=(
            "用户确认后的检索命中（团队接口规范 / 历史接口示例）。"
            "传入时作为「参考规范」参与生成，要求模型按团队约定对齐接口风格。"
        ),
    )


class GeneratePromptsStreamRequest(BaseModel):
    """生成提示词套件的流式请求。"""

    prd_content: str = Field(min_length=1, description="PRD 正文（Markdown）")


class OptimizeDocumentStreamRequest(BaseModel):
    """优化文档的流式请求。"""

    document: str = Field(min_length=1, description="待优化的原文档正文（Markdown）")
    instruction: str = Field(min_length=1, description="用户的优化要求")


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


# ============================================================================
# 认证（登录 / 注册 / 会话）
# ============================================================================


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str = Field(min_length=1, description="用户名")
    password: str = Field(min_length=1, description="密码")


class RegisterRequest(BaseModel):
    """注册请求。

    注册**需要邀请码**——当前是开放注册，但用邀请码挡住随手批量注册的滥用。
    """

    username: str = Field(min_length=3, max_length=32, description="用户名，3-32 个字符")
    password: str = Field(min_length=8, description="密码，至少 8 位")
    invite_code: str = Field(min_length=1, description="邀请码")


class AuthUserResponse(BaseModel):
    """用户基本信息。刻意**不含**任何敏感字段（如密码哈希、salt）。"""

    id: int = Field(description="用户 ID")
    username: str = Field(description="用户名")


class LogoutResponse(BaseModel):
    """登出响应。"""

    ok: bool = Field(default=True, description="是否成功登出")


# ============================================================================
# 方案（solutions）：整份会话快照的持久化
# ============================================================================


class SolutionSaveRequest(BaseModel):
    """新建 / 更新方案的请求体。

    `data` 是 `SessionDataV2` 的 JSON 文本，前端序列化后直接提交，**后端不解析其内容**——
    快照结构属于前端契约（`types/index.ts`），后端把它当不透明字符串存取，
    这样前端调整快照字段时后端不必跟着改。

    `title` / `mode` / `stage` 是随快照一起提交的**冗余摘要**：列表接口只读这三列，
    不必把大 JSON 取出来反序列化。冗余值由前端计算——「产品名该从 summary 还是
    quickInput 取」只有前端清楚。

    这三个字段的长度上限刻意放宽，**不在校验层拒绝超长值**：它们只是展示用的冗余，
    真正的正文在 `data` 里完好无损，为此把整次保存判为 422 是本末倒置——
    用户会看到「保存失败」，而数据其实一点问题都没有。超长值由服务层截断
    （见 `solution_service._clean`）。
    """

    title: str = Field(default="", max_length=2000, description="方案标题，列表展示用")
    mode: str = Field(default="", max_length=200, description="入口模式，列表展示用")
    stage: str = Field(default="", max_length=200, description="当前阶段，列表展示进度用")
    data: str = Field(min_length=1, description="完整会话快照的 JSON 文本")


class SolutionSummary(BaseModel):
    """方案摘要，用于左侧列表。刻意**不含** `data`。"""

    id: int = Field(description="方案 ID")
    title: str = Field(description="方案标题；未取到产品名时由前端给出默认标题")
    mode: str = Field(default="", description="入口模式")
    stage: str = Field(default="", description="当前阶段")
    created_at: str = Field(description="创建时间（UTC ISO 8601）")
    updated_at: str = Field(description="最近更新时间（UTC ISO 8601）")


class SolutionDetail(SolutionSummary):
    """方案详情：摘要 + 全量快照文本。"""

    data: str = Field(description="完整会话快照的 JSON 文本，由前端自行反序列化")


class SolutionListResponse(BaseModel):
    """方案列表响应。

    包一层对象而不是直接返回数组：将来要加分页或总数时不必改响应结构，
    前端也不会因为「根是数组」而在扩展时被迫重写解析。
    """

    solutions: list[SolutionSummary] = Field(
        default_factory=list, description="当前用户的方案摘要列表"
    )


class SolutionDeleteResponse(BaseModel):
    """删除方案响应。"""

    ok: bool = Field(default=True, description="是否成功删除")

