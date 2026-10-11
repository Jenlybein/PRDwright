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

PRD 生成走技能包驱动：额外读取 `skills/prd-generator/references/` 下的字段契约、
六章节模板与写作规范，拼进提示词（见 `_load_skill_artifact` 与 `_build_prd_prompt`）；
技能包缺失时自动降级为 `prompts.py` 里的内置提示词。
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.api.schemas import DocumentResponse, DocumentType, StreamEvent
from backend.services.llm_factory import StreamingChatModel, get_llm
from backend.services.prompts import (
    API_DOCS_GENERATION_PROMPT,
    OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE,
    PRD_GENERATION_PROMPT,
    PRD_SKILL_GENERATION_PROMPT,
    PROMPTS_GENERATION_PROMPT,
    SYSTEM_PROMPT,
)
from backend.services.stream_guard import ensure_not_truncated

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# 技能包资源加载
# ----------------------------------------------------------------------------

# PRD 生成依赖的技能包目录名（仓库根/skills/<名称>/）
PRD_SKILL_NAME = "prd-generator"

# PRD 生成所需的技能资源：`键 -> 技能包内相对路径`
PRD_SKILL_ARTIFACTS: dict[str, str] = {
    "field_schema": "references/field-schema.json",
    "prd_template": "references/prd-template.md",
    "writing_rules": "references/writing-rules.md",
}


def _resolve_skill_dir(skill_name: str) -> Path:
    """定位技能包根目录。

    查找顺序：
    1. 环境变量 `HARNESSPRD_SKILLS_DIR` 指向的目录下查找（部署时后端与 skills
       目录可能不在同一棵目录树里，用它显式指定 skills 根目录）。
    2. 从当前文件向上逐级查找 `<祖先目录>/skills/<skill_name>`。

    Args:
        skill_name: 技能包目录名，如 `prd-generator`。

    Returns:
        技能包根目录路径。找不到时返回按目录层级推算的默认路径，读取时会报错。
    """
    env_dir = os.environ.get("HARNESSPRD_SKILLS_DIR")
    if env_dir:
        return Path(env_dir) / skill_name

    here = Path(__file__).resolve()
    for parent in here.parents[:6]:
        candidate = parent / "skills" / skill_name
        if candidate.is_dir():
            return candidate
    # 兜底：services/ → backend/ → src/ → backend/ → 仓库根
    return here.parents[4] / "skills" / skill_name


@lru_cache(maxsize=None)
def _load_skill_artifact(skill_name: str, relative_path: str) -> str:
    """读取技能包内的资源文件（模板、规范、字段契约等）。

    结果按 `(skill_name, relative_path)` 缓存，避免每轮生成都重复读盘。
    注意：异常不会被缓存，文件补齐后下一次调用会重新读取。

    Args:
        skill_name: 技能包目录名，如 `prd-generator`。
        relative_path: 资源在技能包内的相对路径，如 `references/prd-template.md`。

    Returns:
        文件文本内容（UTF-8，行尾统一为 `\\n`）。

    Raises:
        FileNotFoundError: 技能包目录或资源文件不存在。
    """
    path = _resolve_skill_dir(skill_name) / relative_path
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"技能资源缺失：{path}"
            "（可设置环境变量 HARNESSPRD_SKILLS_DIR 指定 skills 根目录）"
        ) from exc


def _load_prd_skill_artifacts() -> dict[str, str] | None:
    """加载 PRD 生成所需的三份技能资源。

    Returns:
        键为 `field_schema` / `prd_template` / `writing_rules` 的资源字典；
        任一资源缺失时返回 `None`，由调用方降级到内置提示词。
    """
    try:
        return {
            key: _load_skill_artifact(PRD_SKILL_NAME, relative_path)
            for key, relative_path in PRD_SKILL_ARTIFACTS.items()
        }
    except FileNotFoundError:
        logger.warning("PRD 技能资源加载失败，本次改用内置提示词生成", exc_info=True)
        return None

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
    # PRD 提示词组装（技能包驱动）
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prd_prompt(requirements: str, conversation: str) -> tuple[str, str]:
        """组装 PRD 生成的系统提示词与用户消息。

        优先使用 `skills/prd-generator` 技能包的三份资源：
        - `field-schema.json`：作为字段契约放进用户消息，供模型校验输入与归一化；
        - `prd-template.md`：作为章节骨架放进用户消息，约束输出结构；
        - `writing-rules.md`：作为写作规范追加进系统提示词，约束表达与禁编造。

        资源缺失（如部署时未打包 skills 目录）时降级为 `prompts.py` 的内置提示词，
        保证服务不中断。

        Args:
            requirements: 需求澄清结果（问卷答案整理成的文本）。
            conversation: 已格式化好的补充对话记录文本。

        Returns:
            `(系统提示词, 用户消息正文)` 二元组。
        """
        artifacts = _load_prd_skill_artifacts()
        if artifacts is None:
            # 降级：沿用内置提示词
            user_content = DocumentService._load_template("prd").format(
                requirements=requirements,
                conversation=conversation,
            )
            return SYSTEM_PROMPT, user_content

        system_prompt = (
            f"{SYSTEM_PROMPT}\n\n---\n\n"
            "# PRD 写作规范（生成时必须逐条遵守）\n\n"
            f"{artifacts['writing_rules']}"
        )
        user_content = PRD_SKILL_GENERATION_PROMPT.format(
            requirements=requirements,
            conversation=conversation,
            field_schema=artifacts["field_schema"],
            prd_template=artifacts["prd_template"],
        )
        return system_prompt, user_content

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
        system_prompt, user_content = self._build_prd_prompt(
            requirements=requirements,
            conversation=self._format_conversation(history or []),
        )
        messages = self._build_messages(system_prompt, user_content)
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
