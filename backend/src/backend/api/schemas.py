"""API 请求 / 响应模型定义。

路由层只负责参数校验与序列化，业务数据结构统一在这里声明，
避免请求体 / 响应体的字段散落在各个路由文件里。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class ConversationResponse(BaseModel):
    """会话信息响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="会话 ID")
    title: str = Field(default="", description="会话标题")
    status: str = Field(default="active", description="会话状态")
    created_at: datetime | None = Field(default=None, description="创建时间")
    updated_at: datetime | None = Field(default=None, description="更新时间")


class ConversationListResponse(BaseModel):
    """会话列表响应。"""

    items: list[ConversationResponse] = Field(default_factory=list, description="会话列表")
    total: int = Field(default=0, description="会话总数")


class SendMessageRequest(BaseModel):
    """发送消息请求。会话 ID 来自路径参数，不在这里重复声明。"""

    content: str = Field(min_length=1, description="用户输入的消息内容")


# ============================================================================
# SSE 流式响应
# ============================================================================


class StreamEvent(BaseModel):
    """SSE 推送的单个事件。"""

    type: Literal["status", "delta", "tool_call", "document", "error", "done"] = Field(
        description="事件类型：状态提示 / 增量文本 / 工具调用 / 文档更新 / 错误 / 结束"
    )
    content: str = Field(default="", description="文本内容（增量或提示语）")
    data: dict[str, Any] = Field(default_factory=dict, description="事件附加数据")


# ============================================================================
# 文档（document）
# ============================================================================

DocumentType = Literal["prd", "tech_spec", "api_doc"]


class DocumentGenerateRequest(BaseModel):
    """生成文档请求。"""

    doc_type: DocumentType = Field(default="prd", description="文档类型")


class DocumentUpdateRequest(BaseModel):
    """更新文档请求（整篇覆盖）。"""

    content: str = Field(description="文档正文，Markdown 格式")


class DocumentResponse(BaseModel):
    """文档响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="文档 ID")
    conversation_id: str = Field(description="所属会话 ID")
    doc_type: DocumentType = Field(description="文档类型")
    title: str = Field(default="", description="文档标题")
    content: str = Field(default="", description="文档正文，Markdown 格式")
    version: int = Field(default=1, description="文档版本号")
    updated_at: datetime | None = Field(default=None, description="更新时间")
