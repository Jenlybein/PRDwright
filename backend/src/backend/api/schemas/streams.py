"""非任务 SSE 通道的请求体：文档就地优化。

从 `backend.api.schemas` 拆出（#7）；对外引用由包的 `__init__` 再导出。

**三份产物的生成不在这里**：它们只有任务通道（`POST /api/jobs`，请求体是
`JobCreateRequest`）。本模块原先还有四个「直连生成」的请求模型
（`GeneratePrdStreamRequest` / `GeneratePrdFromSummaryStreamRequest` /
`GenerateApiDocsStreamRequest` / `GeneratePromptsStreamRequest`），随四条旧路由一起
退役（#61）——删路由不删服务方法，所以这里只是少了入口模型，能力一点没少。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class OptimizeDocumentStreamRequest(BaseModel):
    """优化文档的流式请求。

    `document` 随请求体带来是**有意的例外**（见 ARCHITECTURE.md §3.3）：就地改写吃的是
    用户眼前那一份正文，可能含还没落库的手改；它属于「改哪一份」这个目标，不是上游。
    """

    document: str = Field(min_length=1, description="待优化的原文档正文（Markdown）")
    instruction: str = Field(min_length=1, description="用户的优化要求")
