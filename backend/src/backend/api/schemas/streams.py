"""文档流式生成（SSE）的请求体：PRD / 接口文档 / 提示词 / 优化。

从 `backend.api.schemas` 拆出（#7）；对外引用由包的 `__init__` 再导出。
"""

from __future__ import annotations

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
    """按方案生成 PRD 的流式请求（旧的非任务通道）。

    **只收方案**，与接口文档 / 提示词两条旧通道同形（spec #51 / C-R3）：需求输入
    （结构化摘要 / 自由文本需求 / 需求澄清线程）由服务端按方案从库里取。请求体里
    不再有 `summary` / `requirements` / `history`——上游与需求只允许有一个来源，
    否则「前端副本落后一版」这类错位会换一条路由复活。

    摘要与自由文本两条入口的差别只体现在**方案上存了哪一列**（`summary_json` 还是
    `quick_requirements`），与请求形状无关。
    """

    solution_id: int = Field(gt=0, description="方案 id：需求输入（摘要 / 自由文本 / 澄清线程）从它取")


class GenerateApiDocsStreamRequest(BaseModel):
    """生成接口文档的流式请求。

    **只收方案**（spec #51 / 票 #54）：上游 PRD 由服务端按方案取（库内当前版本，
    其次方案上保存的录入文本），请求体里不再有 `prd_content`——那是第二条真相，
    上游刚改过就会用旧稿。
    """

    solution_id: int = Field(gt=0, description="方案 id：上游 PRD 从它取")


class GeneratePromptsStreamRequest(BaseModel):
    """生成提示词套件的流式请求：同样只收方案，PRD 与接口文档都由服务端取。"""

    solution_id: int = Field(gt=0, description="方案 id：上游 PRD 与接口文档从它取")


class OptimizeDocumentStreamRequest(BaseModel):
    """优化文档的流式请求。"""

    document: str = Field(min_length=1, description="待优化的原文档正文（Markdown）")
    instruction: str = Field(min_length=1, description="用户的优化要求")
