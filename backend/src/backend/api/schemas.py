"""API 请求 / 响应模型定义。

路由层只负责参数校验与序列化，业务数据结构统一在这里声明，
避免请求体 / 响应体的字段散落在各个路由文件里。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# 这些契约（StreamEvent / QuestionConfig / QuestionsConfig / StepUsage / RunSummary）
# 已下沉到 `core/contracts.py`，此处 re-export 以保持 API 层引用不变：
# `from backend.api.schemas import StreamEvent / QuestionsConfig` 继续可用。
# `RunSummary` 同时是 `run_summary` 事件与 `generation_jobs.usage` 的**同一份结构**。
from backend.core.contracts import (
    DocumentKind,
    QuestionConfig,
    QuestionsConfig,
    RunSummary,
    StepUsage,
    StreamEvent,
)

# ============================================================================
# 问题配置（对应 core/question_config.json）
# ============================================================================

# QuestionConfig / QuestionsConfig 已移至 core/contracts.py（见上方 re-export）。

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
# SSE 流式响应
# ============================================================================

# StreamEvent 已移至 core/contracts.py（见上方 re-export）。

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

    `content_changed` 也是前端算的，理由同上且更硬：快照里同时住着**内容**（摘要、
    对话、粘贴的材料）与**相位**（`viewState` / `activeJobId`）。后端不解析 `data`，
    因此无从分辨「翻了一页」与「改了内容」；而这件事决定了要不要推进
    `content_updated_at`——列表的时间与排序都看它。

    这三个字段的长度上限刻意放宽，**不在校验层拒绝超长值**：它们只是展示用的冗余，
    真正的正文在 `data` 里完好无损，为此把整次保存判为 422 是本末倒置——
    用户会看到「保存失败」，而数据其实一点问题都没有。超长值由服务层截断
    （见 `solution_service._clean`）。
    """

    title: str = Field(default="", max_length=2000, description="方案标题，列表展示用")
    mode: str = Field(default="", max_length=200, description="入口模式，列表展示用")
    stage: str = Field(default="", max_length=200, description="当前阶段，列表展示进度用")
    data: str = Field(min_length=1, description="完整会话快照的 JSON 文本")
    content_changed: bool = Field(
        default=False,
        description=(
            "本次改动是否动了作品内容（而非只是切换阶段 / 翻看）。"
            "为真才推进 `content_updated_at`；缺省为假，即「这次保存不改变内容时间」。"
        ),
    )


class SolutionSummary(BaseModel):
    """方案摘要，用于左侧列表。刻意**不含** `data`。"""

    id: int = Field(description="方案 ID")
    title: str = Field(description="方案标题；未取到产品名时由前端给出默认标题")
    mode: str = Field(default="", description="入口模式")
    stage: str = Field(default="", description="当前阶段")
    created_at: str = Field(description="创建时间（UTC ISO 8601）")
    updated_at: str = Field(
        description="最近一次写库时间（UTC ISO 8601）。注意它**含**「只是翻看」这类保存，"
        "要回答「作品何时真的变了」请用 `content_updated_at`"
    )
    content_updated_at: str = Field(
        description=(
            "最后一次**内容**变更时间（UTC ISO 8601）：需求摘要 / 对话 / 粘贴材料变了，"
            "或三份产物被生成、优化、手改、恢复时推进。列表的时间显示与排序都用它"
        )
    )


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


# ============================================================================
# 文档（documents）：三份产物正文，后端持有
# ============================================================================
# 与 `solutions` 的关系：方案是「一次会话的全量快照」，文档是「某一份产物」。
# 相位（当前阶段、对话记录）留在方案快照里，正文搬到文档表——两者生命周期不同：
# 相位随操作频繁变，正文只在生成或用户手改时变。


class DocumentItem(BaseModel):
    """一份文档的对外结构。

    刻意不给 `id` / `solution_id` / `content_sha`：调用方用「方案 id + 产物类型」
    就能定位一份文档，主键是内部细节；内容指纹是排查用的，不属于对外契约。
    """

    kind: DocumentKind = Field(description="产物类型")
    content: str = Field(
        default="", description="文档正文（Markdown 原文；内部标记块已在后端剥离）"
    )
    pending_items: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "待确认清单：模型产出的结构化元数据，**不由用户编辑**，也不出现在正文里。"
            "旧文档与尚未生成时为空列表。"
        ),
    )
    last_run_summary: dict[str, Any] | None = Field(
        default=None,
        description=(
            "最后一次生成的用量回执。**按不透明对象透传**、不在后端校验字段："
            "迁移自旧快照的回执是前端 camelCase 形状，而新写入的是后端形状，"
            "两者并存；前端已有容错解析器，后端强校验只会把能显示的回执变成 500。"
        ),
    )
    updated_at: str = Field(default="", description="最近更新时间（UTC ISO 8601）")
    stale: bool = Field(
        default=False,
        description="这份下游文档是否基于旧版上游（按 `derived_from` 记录的指纹比较）。"
        "只对接口文档 / 提示词有意义；PRD 没有上游，恒为 false。",
    )
    derived_from: dict[str, Any] | None = Field(
        default=None,
        description="生成这份文档时的上游锚点（`prd` / 可选 `api_docs` 的版本号 + 内容指纹）。"
        "旧数据没有该字段时为 null。",
    )


class DocumentListResponse(BaseModel):
    """某方案下的文档列表。包一层对象，与其它列表响应保持同一约定。"""

    documents: list[DocumentItem] = Field(
        default_factory=list, description="该方案下的文档（至多三份，取决于已生成到哪一步）"
    )


class DocumentSaveRequest(BaseModel):
    """用户手改正文的提交体。

    只有 `content`：归属由路径上的方案 id 决定、产物类型由路径上的 kind 决定。
    刻意**不接收**待确认清单——它是模型产出的元数据，不该由用户或客户端改写。
    """

    content: str = Field(description="用户编辑后的完整文档正文")


# ---------------------------------------------------------------------------
# 文档版本（document_versions）：存为新版本 / 看历史 / 恢复
# ---------------------------------------------------------------------------
# 「正文只住在版本表」之后，documents 行只是一份文档的身份 + 当前版本指针。
# 对外读文档（DocumentItem）的结构**不变**，版本是**新增**的一组读 / 写能力。


class DocumentVersionItem(BaseModel):
    """版本摘要，用于版本列表。刻意**不含正文**——列表可能很长，只给概览。"""

    version_id: int = Field(description="版本 id（供恢复引用）")
    version_no: int = Field(description="版本序号，从 1 开始、单调递增")
    source: str = Field(
        description="这一版的来源：prd / api_docs / prompts（整篇生成）、optimize / clarify（就地改写）、"
        "manual（手改）、checkpoint（用户存为新版本）、restore（恢复）、migrated（迁移回填）"
    )
    note: str = Field(default="", description="「存为新版本」时用户写的备注；其它来源为空")
    content_sha: str = Field(default="", description="内容指纹（排查用，不属于展示契约）")
    content_length: int = Field(default=0, description="正文字符数（概览用，不下发正文）")
    created_at: str = Field(description="创建时间（UTC ISO 8601）")
    is_current: bool = Field(default=False, description="是否为当前版本")


class DocumentVersionDetail(BaseModel):
    """版本详情：摘要 + 正文 + 清单 + 回执。"""

    version_id: int = Field(description="版本 id")
    version_no: int = Field(description="版本序号")
    content: str = Field(default="", description="该版本文档正文（Markdown）")
    pending_items: list[dict[str, Any]] = Field(
        default_factory=list, description="该版本当时的待确认清单"
    )
    last_run_summary: dict[str, Any] | None = Field(
        default=None, description="该版本当时的用量回执；缺失为 null"
    )
    source: str = Field(description="这一版的来源（见 DocumentVersionItem.source）")
    note: str = Field(default="", description="备注")
    created_at: str = Field(description="创建时间（UTC ISO 8601）")
    updated_at: str = Field(description="最近更新时间（UTC ISO 8601）")
    is_current: bool = Field(default=False, description="是否为当前版本")


class DocumentVersionListResponse(BaseModel):
    """某份文档的版本列表。包一层对象，与其它列表响应保持同一约定。"""

    versions: list[DocumentVersionItem] = Field(
        default_factory=list, description="版本摘要，按版本号从新到旧"
    )


class DocumentCheckpointRequest(BaseModel):
    """把当前正文「存为一个新版本」的提交体。

    `content` 由客户端给出（取用户**眼前**这一版）——防抖窗口内用户刚敲的字可能还没落库，
    直接存库里那份会丢掉它。清单与回执则从服务端当前版本继承，不由客户端改。
    """

    content: str = Field(description="要存为新版本的完整文档正文")
    note: str = Field(default="", max_length=200, description="这一版的备注，可有可无")


class DocumentRestoreRequest(BaseModel):
    """恢复某个历史版本的提交体。"""

    version_id: int = Field(description="要恢复的历史版本 id")


class DocumentVersionDeleteResponse(BaseModel):
    """删除某个历史版本的响应。

    刻意回 `version_id`（而不仅是 `ok`）：前端可能在多处同时持有版本引用，回一个被删掉
    的 id 让调用方有机会把界面上那条记录一并收掉，不必再猜「我刚才删的是哪一版」。
    """

    ok: bool = Field(default=True, description="是否成功删除")
    version_id: int = Field(description="被删除的版本 id")


# ============================================================================
# 生成任务（generation_jobs）：把一次生成持久化并支持刷新重连
# ============================================================================

# 任务类型。前三种是「整篇生成」，与前端 `DocumentKind` 一一对应；
# `optimize`（按用户指令就地优化）、`clarify`（按澄清对话就地改写）与
# `auto_clarify`（AI 自行拍板待确认项后就地改写）都是**对某一份已有文档的动作**，
# 因此它们必须额外带 `doc_kind` 才能落库。
JobKind = Literal["prd", "api_docs", "prompts", "optimize", "clarify", "auto_clarify"]


class JobCreateRequest(BaseModel):
    """创建一次生成任务的请求体。

    一个模型承载五种任务：必填字段按 `kind` 区分（路由层校验），
    因此不同 `kind` 只用到各自相关的字段，其余留默认值即可。
    """

    kind: JobKind = Field(
        description="任务类型：prd / api_docs / prompts / optimize / clarify / auto_clarify"
    )
    solution_id: int | None = Field(
        default=None,
        description="关联方案 id；快捷入口等场景可为空（逃生口）",
    )
    doc_kind: DocumentKind | None = Field(
        default=None,
        description=(
            "本次任务**作用在**哪一份产物上。`optimize` 与 `clarify` 必须给：它们的 `kind`"
            "表达的是「做了一件什么事」，而落库要知道「东西写进哪一份文档」。缺省时不落库"
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

