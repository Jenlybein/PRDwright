"""文档流式生成（SSE）的请求体：PRD / 接口文档 / 提示词 / 优化。

从 `backend.api.schemas` 拆出（#7）；对外引用由包的 `__init__` 再导出。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .conversation import MessageItem

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
