"""会话业务服务。

负责把问卷答案整理成模型可读的上下文、维护会话历史，并以事件流的形式
把模型回复逐字推送给前端。路由层只做参数解析，业务逻辑集中在这里。

对前端暴露的消息格式统一为 `FrontendMessage`（`{"role": ..., "content": ...}`），
`_build_lc_messages()` 负责转成 LangChain 消息，`_serialize_messages()` 负责转回来。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Literal, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage

from backend.api.schemas import ConversationResponse, QuestionsConfig, StreamEvent
from backend.services.llm_factory import get_llm
from backend.services.prompts import SYSTEM_PROMPT

# 问卷配置文件：backend/src/backend/core/question_config.json
QUESTION_CONFIG_PATH = Path(__file__).resolve().parents[1] / "core" / "question_config.json"

# ----------------------------------------------------------------------------
# 前端消息格式
# ----------------------------------------------------------------------------

# 角色取值，与前端聊天列表保持一致
MessageRole = Literal["system", "user", "assistant"]


class FrontendMessage(TypedDict):
    """前端聊天消息结构。

    Attributes:
        role: 消息角色，`system` / `user` / `assistant`。
        content: 消息正文。
    """

    role: MessageRole
    content: str


# 常见角色别名 → 标准角色，兼容前端或模型侧的不同写法
_ROLE_ALIASES: dict[str, MessageRole] = {
    "system": "system",
    "developer": "system",
    "user": "user",
    "human": "user",
    "assistant": "assistant",
    "ai": "assistant",
    "model": "assistant",
    "bot": "assistant",
}

# 首轮开场白：把问卷答案交给模型，让它先做理解确认与追问，而不是直接写 PRD
REQUIREMENTS_WRAPPER = """我填写了一份需求问卷，内容如下：

{requirements}

请你先做两件事：
1. 用 3-5 条要点简要复述你对这个产品的理解，确认我们对齐了。
2. 针对仍然模糊、会明显影响后续 PRD 质量的关键点向我提问，一次问 3-5 个，不要贪多。

这一轮不要输出 PRD 正文，只做理解确认与追问。"""


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------


def _normalize_role(role: Any) -> MessageRole:
    """把任意写法的角色名归一化成标准角色，无法识别时按 `user` 处理。"""
    return _ROLE_ALIASES.get(str(role).strip().lower(), "user")


def _normalize_value(value: Any) -> str:
    """把问卷答案的任意取值转成展示文本。

    - `None` / 空字符串 → 空串（调用方据此跳过该题）
    - 布尔 → 是 / 否
    - 列表 / 元组 / 集合 → 用「、」连接（多选题的答案）
    - 字典 → JSON 字符串
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple, set)):
        return "、".join(str(item) for item in value if str(item))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _extract_text(content: Any) -> str:
    """从消息内容里取出纯文本。

    部分供应商的分块内容不是字符串，而是 `[{"type": "text", "text": "..."}]`
    这样的结构化列表，这里统一摊平成字符串。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            # 形如 {"type": "text", "text": "..."}
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return "" if content is None else str(content)


def load_questions_config(path: Path | None = None) -> QuestionsConfig | None:
    """读取问卷配置，用于把答案的 key 还原成题目文案。

    读取失败时返回 `None`，调用方退化成直接展示字段名，不影响主流程。

    Args:
        path: 配置文件路径，默认取 `core/question_config.json`。

    Returns:
        问卷配置；文件缺失或内容非法时为 `None`。
    """
    path = path or QUESTION_CONFIG_PATH
    try:
        raw = path.read_text(encoding="utf-8")
        return QuestionsConfig.model_validate_json(raw)
    except (FileNotFoundError, ValueError):
        return None


def format_form_data(
    form_data: dict[str, Any],
    questions: QuestionsConfig | None = None,
) -> str:
    """把问卷答案字典转成易读的 Markdown 文本。

    题目顺序和文案优先取自问卷配置（`name` → `label`）；配置里没有的字段
    追加在末尾，直接用字段名展示，避免答案被静默丢弃。空值题目不输出。

    Args:
        form_data: 问卷答案，key 为 `QuestionConfig.name`，value 为任意类型。
        questions: 问卷配置；不传则自动读取配置文件。

    Returns:
        Markdown 无序列表文本，每形如 `- **题目文案**：答案`。
    """
    if not form_data:
        return "（用户尚未填写任何需求信息）"

    if questions is None:
        questions = load_questions_config()

    # 建立 name → label 映射，并保留配置里的题目顺序
    ordered: list[tuple[str, str]] = []
    if questions is not None:
        for question in [*questions.base_questions, *questions.advanced_questions]:
            ordered.append((question.name, question.label))

    lines: list[str] = []
    handled: set[str] = set()

    for name, label in ordered:
        if name not in form_data:
            continue
        handled.add(name)
        text = _normalize_value(form_data[name])
        if text:
            lines.append(f"- **{label}**：{text}")

    # 配置之外的字段（例如前端后续新增的题目）
    for key, value in form_data.items():
        if key in handled:
            continue
        text = _normalize_value(value)
        if text:
            lines.append(f"- **{key}**：{text}")

    return "\n".join(lines) if lines else "（用户尚未填写任何需求信息）"


def _build_lc_messages(
    system_prompt: str = SYSTEM_PROMPT,
    history: Iterable[FrontendMessage | dict[str, Any]] | None = None,
    user_message: str | None = None,
) -> list[BaseMessage]:
    """把系统提示词、历史消息与本轮输入组装成 LangChain 消息列表。

    历史里的 `system` 消息会原样保留（放在系统提示词之后），`user` 转
    `HumanMessage`、`assistant` 转 `AIMessage`，以此保证 user / assistant
    严格交替——Anthropic 对消息顺序有硬性要求。

    Args:
        system_prompt: 系统提示词，默认取 `prompts.SYSTEM_PROMPT`。
        history: 历史消息，元素为 `FrontendMessage` 或等价字典。
        user_message: 本轮用户输入；为空则不追加。

    Returns:
        LangChain 消息列表，首条固定为 `SystemMessage`。
    """
    messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]

    for item in history or []:
        role = _normalize_role(item.get("role"))
        content = str(item.get("content", ""))
        if not content:
            continue
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))
        else:
            messages.append(SystemMessage(content=content))

    # 历史末尾与本轮输入完全相同时不重复追加，避免同句话连发两次
    if user_message:
        last = messages[-1] if len(messages) > 1 else None
        if not (isinstance(last, HumanMessage) and last.content == user_message):
            messages.append(HumanMessage(content=user_message))

    return messages


def _serialize_messages(
    messages: Iterable[BaseMessage],
    include_system: bool = False,
) -> list[FrontendMessage]:
    """把 LangChain 消息转换回前端消息格式。

    Args:
        messages: LangChain 消息列表。
        include_system: 是否保留系统消息；默认丢弃，因为系统提示词不需要
            展示在聊天界面里。

    Returns:
        `FrontendMessage` 列表，内容为空的消息会被跳过。
    """
    result: list[FrontendMessage] = []

    for message in messages:
        if isinstance(message, SystemMessage):
            role: MessageRole = "system"
        elif isinstance(message, (AIMessage, AIMessageChunk)):
            role = "assistant"
        elif isinstance(message, HumanMessage):
            role = "user"
        else:
            role = _normalize_role(getattr(message, "type", "user"))

        if role == "system" and not include_system:
            continue

        content = _extract_text(message.content)
        if not content:
            continue

        result.append({"role": role, "content": content})

    return result


# ----------------------------------------------------------------------------
# 会话记录
# ----------------------------------------------------------------------------


class ConversationNotFoundError(RuntimeError):
    """会话 ID 不存在。"""


@dataclass
class _ConversationRecord:
    """内存中的会话记录。当前没有接数据库，进程重启后数据丢失。"""

    id: str
    title: str
    status: str = "active"
    form_data: dict[str, Any] = field(default_factory=dict)
    requirements: str = ""
    messages: list[FrontendMessage] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ConversationService:
    """会话服务。

    职责：管理会话上下文、把问卷答案整理成模型上下文、调用 LLM 并以
    `StreamEvent` 事件流的形式逐字返回回复。

    Attributes:
        system_prompt: 系统提示词，默认取 `prompts.SYSTEM_PROMPT`。
    """

    def __init__(
        self,
        llm: BaseChatModel | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        """初始化服务。

        Args:
            llm: 自定义 LLM 实例；不传则在首次使用时通过 `get_llm()` 惰性创建。
            system_prompt: 系统提示词。
        """
        self.system_prompt = system_prompt
        self._llm = llm
        self._conversations: dict[str, _ConversationRecord] = {}

    # ------------------------------------------------------------------
    # LLM 惰性获取
    # ------------------------------------------------------------------

    @property
    def llm(self) -> BaseChatModel:
        """LLM 实例，首次访问时才创建（避免在服务初始化阶段就要求密钥）。"""
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    # ------------------------------------------------------------------
    # 会话增删改查
    # ------------------------------------------------------------------

    @staticmethod
    def _new_id() -> str:
        """生成会话 ID。"""
        return uuid.uuid4().hex[:12]

    @staticmethod
    def _guess_title(form_data: dict[str, Any], fallback: str = "") -> str:
        """从答案里猜一个会话标题：优先产品名，其次问题描述的前 20 个字。"""
        if fallback:
            return fallback
        name = _normalize_value(form_data.get("product_name"))
        if name:
            return name
        problem = _normalize_value(form_data.get("problem_to_solve"))
        if problem:
            return problem[:20]
        return "未命名会话"

    def _create_record(
        self,
        form_data: dict[str, Any],
        title: str = "",
    ) -> _ConversationRecord:
        """创建并登记一条会话记录。"""
        now = datetime.now()
        record = _ConversationRecord(
            id=self._new_id(),
            title=self._guess_title(form_data, title),
            form_data=dict(form_data),
            requirements=format_form_data(form_data),
            created_at=now,
            updated_at=now,
        )
        self._conversations[record.id] = record
        return record

    @staticmethod
    def _to_response(record: _ConversationRecord) -> ConversationResponse:
        """把内存记录转成响应模型。"""
        return ConversationResponse.model_validate(record)

    def create_conversation(
        self,
        title: str = "",
        form_data: dict[str, Any] | None = None,
    ) -> ConversationResponse:
        """创建会话并保存问卷答案。

        Args:
            title: 会话标题，留空则根据答案自动推断。
            form_data: 问卷答案。

        Returns:
            新建会话的信息。
        """
        return self._to_response(self._create_record(form_data or {}, title))

    def get_conversation(self, conversation_id: str) -> ConversationResponse:
        """按 ID 获取会话。

        Raises:
            ConversationNotFoundError: 会话 ID 不存在。
        """
        record = self._conversations.get(conversation_id)
        if record is None:
            raise ConversationNotFoundError(f"会话不存在：{conversation_id}")
        return self._to_response(record)

    def list_conversations(self) -> list[ConversationResponse]:
        """获取会话列表，按更新时间倒序。"""
        records = sorted(
            self._conversations.values(),
            key=lambda r: r.updated_at or r.created_at or datetime.min,
            reverse=True,
        )
        return [self._to_response(r) for r in records]

    def get_messages(self, conversation_id: str) -> list[FrontendMessage]:
        """获取会话的完整消息历史（前端格式）。

        Raises:
            ConversationNotFoundError: 会话 ID 不存在。
        """
        record = self._conversations.get(conversation_id)
        if record is None:
            raise ConversationNotFoundError(f"会话不存在：{conversation_id}")
        return list(record.messages)

    # ------------------------------------------------------------------
    # 流式对话
    # ------------------------------------------------------------------

    async def _astream_text(self, messages: list[BaseMessage]) -> AsyncIterator[str]:
        """调用模型流式生成，逐段产出纯文本。"""
        async for chunk in self.llm.astream(messages):
            text = _extract_text(chunk.content)
            if text:
                yield text

    async def _stream_reply(
        self,
        messages: list[BaseMessage],
        status_text: str | None = None,
        on_finish: Any = None,
        extra_data: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """通用的流式回复流程：状态提示 → 逐字增量 → 收尾事件。

        Args:
            messages: 已组装好的 LangChain 消息。
            status_text: 首个状态事件的提示语；为 `None` 时不发状态事件
                （调用方已经发过时用）。
            on_finish: 生成结束后的回调，接收完整回复文本，用于回写历史。
            extra_data: 附加到 `done` 事件的数据，如会话 ID。

        Yields:
            `StreamEvent`，依次为 `status` / 若干 `chunk` / `done`，出错时为 `error`。
        """
        if status_text:
            yield StreamEvent(type="status", content=status_text)

        chunks: list[str] = []
        try:
            async for text in self._astream_text(messages):
                chunks.append(text)
                yield StreamEvent(type="chunk", content=text)
        except Exception as exc:  # noqa: BLE001 - 流式接口需要把异常转成事件而不是抛出
            yield StreamEvent(
                type="error",
                content=f"生成失败：{exc}",
                data={"error_type": type(exc).__name__},
            )
            return

        reply = "".join(chunks).strip()
        if on_finish is not None:
            on_finish(reply)

        # done 事件带上完整回复，便于前端在断流或丢包时做一次对齐
        data: dict[str, Any] = {**(extra_data or {}), "reply": reply}
        yield StreamEvent(type="done", content="", data=data)

    async def start_conversation_stream(
        self,
        form_data: dict[str, Any],
        title: str = "",
        opening_prompt: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """提交问卷后开启会话，流式返回模型的第一轮回复。

        流程：登记会话 → 把问卷答案整理成需求文本 → 让模型先做理解确认与
        追问 → 逐字推送 → 把这一轮写进会话历史。

        Args:
            form_data: 问卷答案。
            title: 会话标题，留空则自动推断。
            opening_prompt: 自定义开场提示词；不传则用 `REQUIREMENTS_WRAPPER`。

        Yields:
            `StreamEvent`。首个 `status` 事件的 `data` 里带 `conversation_id`，
            前端需要立刻记下来，后续接续对话要靠它。
        """
        record = self._create_record(form_data, title)
        yield StreamEvent(
            type="status",
            content="已收到你的需求，正在整理分析…",
            data={"conversation_id": record.id},
        )

        # 首轮的用户消息：需求文本 + 让模型先确认理解再追问
        first_message = (opening_prompt or REQUIREMENTS_WRAPPER).format(
            requirements=record.requirements
        )
        messages = _build_lc_messages(
            system_prompt=self.system_prompt,
            user_message=first_message,
        )

        def _persist(reply: str) -> None:
            """把首轮问答写进历史，供后续接续对话使用。"""
            record.messages.append({"role": "user", "content": first_message})
            record.messages.append({"role": "assistant", "content": reply})
            record.updated_at = datetime.now()

        async for event in self._stream_reply(
            messages,
            on_finish=_persist,
            extra_data={"conversation_id": record.id},
        ):
            yield event

    async def continue_conversation_stream(
        self,
        user_message: str,
        conversation_id: str | None = None,
        history: list[FrontendMessage] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """接续对话，流式返回模型对本轮输入的回复。

        支持两种用法：
        - **有状态**：传 `conversation_id`，历史从服务内存里取（默认）。
        - **无状态**：不传 `conversation_id` 而传 `history`，由前端携带完整历史，
          适合服务重启或多实例部署的场景。

        Args:
            user_message: 本轮用户输入。
            conversation_id: 会话 ID；给出时优先使用服务内的历史，并在生成
                结束后把本轮问答写回该会话。
            history: 前端传入的历史消息；传入时优先于服务端历史。

        Yields:
            `StreamEvent`。`done` 事件的 `data` 带 `conversation_id` 与完整
            `reply`，前端可用它做一次最终对齐。
        """
        record = self._conversations.get(conversation_id) if conversation_id else None

        if conversation_id and record is None:
            yield StreamEvent(
                type="error",
                content=f"会话不存在或已过期：{conversation_id}",
                data={"conversation_id": conversation_id},
            )
            return

        # 前端显式传入历史时以它为准，否则用服务内存里的历史
        base_history: list[FrontendMessage] = list(history) if history else list(
            record.messages if record else []
        )
        messages = _build_lc_messages(
            system_prompt=self.system_prompt,
            history=base_history,
            user_message=user_message,
        )

        def _persist(reply: str) -> None:
            """把本轮问答写回会话历史。"""
            if record is not None:
                record.messages.append({"role": "user", "content": user_message})
                record.messages.append({"role": "assistant", "content": reply})
                record.updated_at = datetime.now()
            else:
                base_history.append({"role": "user", "content": user_message})
                base_history.append({"role": "assistant", "content": reply})

        async for event in self._stream_reply(
            messages,
            status_text="正在思考…",
            on_finish=_persist,
            extra_data={"conversation_id": conversation_id or ""},
        ):
            yield event


# ----------------------------------------------------------------------------
# 模块级便捷入口：路由层直接调用这些函数，内部委托给默认服务实例
# ----------------------------------------------------------------------------

_default_service: ConversationService | None = None


def get_service() -> ConversationService:
    """获取默认会话服务实例（懒加载单例）。"""
    global _default_service
    if _default_service is None:
        _default_service = ConversationService()
    return _default_service


async def create_conversation(
    title: str = "",
    answers: dict[str, Any] | None = None,
) -> ConversationResponse:
    """创建会话并初始化上下文。"""
    return get_service().create_conversation(title=title, form_data=answers or {})


async def get_conversation(conversation_id: str) -> ConversationResponse:
    """按 ID 获取会话详情。"""
    return get_service().get_conversation(conversation_id)


async def list_conversations() -> list[ConversationResponse]:
    """获取会话列表。"""
    return get_service().list_conversations()


async def stream_reply(
    conversation_id: str,
    content: str,
) -> AsyncIterator[StreamEvent]:
    """处理用户消息，以事件流形式逐步产出回复。"""
    async for event in get_service().continue_conversation_stream(
        user_message=content,
        conversation_id=conversation_id,
    ):
        yield event


async def start_stream(
    form_data: dict[str, Any],
    title: str = "",
) -> AsyncIterator[StreamEvent]:
    """提交问卷答案、创建会话，并以事件流形式产出模型的第一轮回复。

    Args:
        form_data: 问卷答案。
        title: 会话标题，留空则自动推断。

    Yields:
        `StreamEvent`，首个 `status` 事件的 `data` 里带 `conversation_id`。
    """
    async for event in get_service().start_conversation_stream(
        form_data=form_data,
        title=title,
    ):
        yield event


async def continue_stream(
    user_message: str,
    conversation_id: str | None = None,
    history: list[FrontendMessage] | None = None,
) -> AsyncIterator[StreamEvent]:
    """接续对话，以事件流形式产出模型对本轮输入的回复。

    Args:
        user_message: 本轮用户输入。
        conversation_id: 会话 ID；为 `None` 时走无状态模式。
        history: 前端回传的历史消息；传入时优先于服务端保存的历史。

    Yields:
        `StreamEvent`，`done` 事件的 `data` 带 `conversation_id` 与完整 `reply`。
    """
    async for event in get_service().continue_conversation_stream(
        user_message=user_message,
        conversation_id=conversation_id,
        history=history,
    ):
        yield event
