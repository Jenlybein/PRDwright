"""文档业务服务（尚未实现）。

负责 PRD / 技术方案 / 接口文档的生成、读取与更新。
目前所有函数都是占位，调用会抛出 NotImplementedError。
"""

from __future__ import annotations

from backend.api.schemas import DocumentResponse, DocumentType


async def generate_document(
    conversation_id: str,
    doc_type: DocumentType = "prd",
) -> DocumentResponse:
    """基于会话上下文生成指定类型的文档。"""
    raise NotImplementedError("document_service.generate_document 尚未实现")


async def get_document(
    conversation_id: str,
    doc_type: DocumentType = "prd",
) -> DocumentResponse:
    """读取指定会话下的文档。"""
    raise NotImplementedError("document_service.get_document 尚未实现")


async def update_document(
    conversation_id: str,
    doc_type: DocumentType,
    content: str,
) -> DocumentResponse:
    """整篇更新文档内容。"""
    raise NotImplementedError("document_service.update_document 尚未实现")
