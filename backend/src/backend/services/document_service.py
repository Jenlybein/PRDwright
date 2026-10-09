"""文档业务服务。

负责把对话澄清结果整理成可落地的文档：PRD / 接口文档 / 提示词套件，并支持
对已有文档做优化改写。所有生成都走 LLM 的 `astream` 流式接口，以
`StreamEvent` 事件流的形式逐字推送，与 `conversation_service` 的流式约定一致。

对外暴露两类接口：
- **流式生成**（`*_stream`，供路由层 `StreamingResponse` 消费）：
  - `generate_prd_stream`：根据对话历史 + 问卷答案生成 PRD。
  - `generate_api_docs_stream`：根据 PRD 正文生成接口文档。
  - `generate_prompts_stream`：根据 PRD 正文生成提示词套件。
  - `optimize_document_stream`：按用户指令优化既有文档。
- **同步占位**（`generate_document` / `get_document` / `update_document`）：
  为兼容旧的路由契约保留，当前尚未实现，调用会抛出 `NotImplementedError`。

提示词模板集中在 `services/prompts.py`，本模块只负责「加载模板 + 拼接变量
+ 调用模型流式生成」，不维护大段提示词文本。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.api.schemas import DocumentResponse, DocumentType, StreamEvent
from backend.services.llm_factory import StreamingChatModel, get_llm
from backend.services.prompts import (
    API_DOCS_GENERATION_PROMPT,
    OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE,
    PRD_GENERATION_PROMPT,
    PROMPTS_GENERATION_PROMPT,
    SYSTEM_PROMPT,
)
from backend.services.stream_guard import ensure_not_truncated

class DocumentService:
    """文档生成 / 优化服务。

    职责单一：把输入材料（对话、问卷、已有文档）拼进提示词模板，交给 LLM
    流式生成，并包装成 `StreamEvent` 事件流。会话与文档的持久化不在本服务
    内，调用方负责传入完整上下文。
    """

    def __init__(self, llm: StreamingChatModel | None = None) -> None:
        """初始化服务。

        Args:
            llm: 自定义 LLM 实例；不传则在首次使用时通过 `get_llm()` 惰性创建。
        """
        self._llm = llm

    # ------------------------------------------------------------------
    # LLM 惰性获取
    # ------------------------------------------------------------------

    @property
    def llm(self) -> StreamingChatModel:
        """LLM 实例，首次访问时才创建（避免服务初始化阶段就要求密钥）。"""
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    # ------------------------------------------------------------------
    # 模板加载与 Prompt 拼接
    # ------------------------------------------------------------------

    @staticmethod
    def _load_template(name: str) -> str:
        """按名称返回提示词模板。

        模板统一收口在 `services/prompts.py`，这里提供按需取用的入口，便于
        后续扩展（如从文件加载自定义模板）。

        Args:
            name: 模板名，取值 `prd` / `api_doc` / `prompts` / `optimize`。

        Returns:
            模板字符串，占位符为 `str.format()` 风格。

        Raises:
            ValueError: 传入了未知的模板名。
        """
        templates: dict[str, str] = {
            "prd": PRD_GENERATION_PROMPT,
            "api_doc": API_DOCS_GENERATION_PROMPT,
            "prompts": PROMPTS_GENERATION_PROMPT,
            "optimize": OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE,
        }
        if name not in templates:
            raise ValueError(f"未知的文档模板：{name}")
        return templates[name]

    @staticmethod
    def _format_conversation(history: list[dict[str, Any]]) -> str:
        """把对话历史（`{role, content}` 列表）拼成易读的 Markdown 文本。

        Args:
            history: 前端回传的对话消息，元素形如 `{"role": ..., "content": ...}`。

        Returns:
            形如 `**用户**：...` / `**AI**：...` 的多行文本；空历史返回占位文案。
        """
        if not history:
            return "（无补充对话记录）"

        lines: list[str] = []
        for item in history:
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            speaker = "用户" if role == "user" else "AI" if role == "assistant" else role or "系统"
            lines.append(f"**{speaker}**：{content}")
        return "\n\n".join(lines) if lines else "（无补充对话记录）"

    def _build_messages(
        self,
        system_prompt: str,
        user_content: str,
    ) -> list[BaseMessage]:
        """把系统提示词与用户输入组装成 LangChain 消息列表。

        Args:
            system_prompt: 系统提示词。
            user_content: 已填充好变量的用户消息正文。

        Returns:
            `[SystemMessage, HumanMessage]` 两条消息。
        """
        return [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ]

    # ------------------------------------------------------------------
    # 通用流式生成
    # ------------------------------------------------------------------

    async def _stream_reply(
        self,
        messages: list[BaseMessage],
        status_text: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """通用流式生成流程：状态提示 → 逐字增量 → 收尾事件。

        Args:
            messages: 已组装好的 LangChain 消息。
            status_text: 首个状态事件的提示语；为 `None` 时不发状态事件。
            extra_data: 附加到 `done` 事件的数据（如文档类型）。

        Yields:
            `StreamEvent`，依次为 `status` / 若干 `chunk` / `done`，出错时为 `error`。
        """
        if status_text:
            yield StreamEvent(type="status", content=status_text)

        chunks: list[str] = []
        try:
            last_chunk = None
            async for chunk in self.llm.astream(messages):
                text = self._extract_text(chunk.content)
                last_chunk = chunk
                if text:
                    chunks.append(text)
                    yield StreamEvent(type="chunk", content=text)
            # 流结束后检查是否因 max_tokens 被截断，是则抛出转 error 事件
            if last_chunk is not None:
                ensure_not_truncated(last_chunk)
        except Exception as exc:  # noqa: BLE001 - 流式接口需把异常转成事件而非抛出
            yield StreamEvent(
                type="error",
                content=f"生成失败：{exc}",
                data={"error_type": type(exc).__name__},
            )
            return

        reply = "".join(chunks).strip()
        data: dict[str, Any] = {**(extra_data or {}), "reply": reply}
        yield StreamEvent(type="done", content="", data=data)

    @staticmethod
    def _extract_text(content: Any) -> str:
        """从分块内容里取出纯文本，兼容字符串与结构化列表两种格式。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)
        return "" if content is None else str(content)

    # ------------------------------------------------------------------
    # 三个流式生成方法
    # ------------------------------------------------------------------

    async def generate_prd_stream(
        self,
        requirements: str,
        history: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """根据需求澄清结果与对话记录生成 PRD。

        Args:
            requirements: 需求澄清结果（问卷答案整理成的文本）。
            history: 补充对话记录（`{role, content}` 列表），可为空。

        Yields:
            `StreamEvent`，`done` 事件的 `data` 带完整 `reply`（PRD 正文）。
        """
        user_content = self._load_template("prd").format(
            requirements=requirements,
            conversation=self._format_conversation(history or []),
        )
        messages = self._build_messages(SYSTEM_PROMPT, user_content)
        async for event in self._stream_reply(
            messages,
            status_text="正在生成 PRD…",
            extra_data={"doc_type": "prd"},
        ):
            yield event

    async def generate_api_docs_stream(
        self,
        prd_content: str,
    ) -> AsyncIterator[StreamEvent]:
        """根据 PRD 正文生成接口文档。

        Args:
            prd_content: PRD 正文（Markdown）。

        Yields:
            `StreamEvent`，`done` 事件的 `data` 带完整 `reply`（接口文档正文）。
        """
        user_content = self._load_template("api_doc").format(prd_content=prd_content)
        messages = self._build_messages(SYSTEM_PROMPT, user_content)
        async for event in self._stream_reply(
            messages,
            status_text="正在生成接口文档…",
            extra_data={"doc_type": "api_doc"},
        ):
            yield event

    async def generate_prompts_stream(
        self,
        prd_content: str,
    ) -> AsyncIterator[StreamEvent]:
        """根据 PRD 正文生成提示词套件。

        Args:
            prd_content: PRD 正文（Markdown）。

        Yields:
            `StreamEvent`，`done` 事件的 `data` 带完整 `reply`（提示词套件正文）。
        """
        user_content = self._load_template("prompts").format(prd_content=prd_content)
        messages = self._build_messages(SYSTEM_PROMPT, user_content)
        async for event in self._stream_reply(
            messages,
            status_text="正在生成提示词套件…",
            extra_data={"doc_type": "prompts"},
        ):
            yield event

    async def optimize_document_stream(
        self,
        document: str,
        instruction: str,
    ) -> AsyncIterator[StreamEvent]:
        """按用户指令优化既有文档。

        Args:
            document: 原文档正文（Markdown）。
            instruction: 用户的优化要求。

        Yields:
            `StreamEvent`，`done` 事件的 `data` 带完整 `reply`（优化后的文档正文）。
        """
        user_content = self._load_template("optimize").format(
            instruction=instruction,
            document=document,
        )
        messages = self._build_messages(SYSTEM_PROMPT, user_content)
        async for event in self._stream_reply(
            messages,
            status_text="正在优化文档…",
            extra_data={"doc_type": "optimize"},
        ):
            yield event


# ----------------------------------------------------------------------------
# 模块级便捷入口：路由层直接调用这些函数，内部委托给默认服务实例
# ----------------------------------------------------------------------------

_default_service: DocumentService | None = None


def get_service() -> DocumentService:
    """获取默认文档服务实例（懒加载单例）。"""
    global _default_service
    if _default_service is None:
        _default_service = DocumentService()
    return _default_service


async def generate_prd_stream(
    requirements: str,
    history: list[dict[str, Any]] | None = None,
) -> AsyncIterator[StreamEvent]:
    """生成 PRD 的流式入口。"""
    async for event in get_service().generate_prd_stream(
        requirements=requirements,
        history=history,
    ):
        yield event


async def generate_api_docs_stream(
    prd_content: str,
) -> AsyncIterator[StreamEvent]:
    """生成接口文档的流式入口。"""
    async for event in get_service().generate_api_docs_stream(prd_content=prd_content):
        yield event


async def generate_prompts_stream(
    prd_content: str,
) -> AsyncIterator[StreamEvent]:
    """生成提示词套件的流式入口。"""
    async for event in get_service().generate_prompts_stream(prd_content=prd_content):
        yield event


async def optimize_document_stream(
    document: str,
    instruction: str,
) -> AsyncIterator[StreamEvent]:
    """优化文档的流式入口。"""
    async for event in get_service().optimize_document_stream(
        document=document,
        instruction=instruction,
    ):
        yield event


# ----------------------------------------------------------------------------
# 同步占位接口（兼容旧路由契约，尚未实现）
# ----------------------------------------------------------------------------


async def generate_document(
    conversation_id: str,
    doc_type: DocumentType = "prd",
) -> DocumentResponse:
    """基于会话上下文生成指定类型的文档（尚未实现）。"""
    raise NotImplementedError("document_service.generate_document 尚未实现")


async def get_document(
    conversation_id: str,
    doc_type: DocumentType = "prd",
) -> DocumentResponse:
    """读取指定会话下的文档（尚未实现）。"""
    raise NotImplementedError("document_service.get_document 尚未实现")


async def update_document(
    conversation_id: str,
    doc_type: DocumentType,
    content: str,
) -> DocumentResponse:
    """整篇更新文档内容（尚未实现）。"""
    raise NotImplementedError("document_service.update_document 尚未实现")
