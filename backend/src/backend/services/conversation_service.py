"""会话业务服务。

负责把问卷答案整理成模型可读的上下文、维护会话历史，并以事件流的形式
把模型回复逐字推送给前端。路由层只做参数解析，业务逻辑集中在这里。

对前端暴露的消息格式统一为 `FrontendMessage`（`{"role": ..., "content": ...}`），
`_build_lc_messages()` 负责把历史与本轮输入组装成 LangChain 消息列表。

系统人设与首轮开场白来自 `services/prompts.py` 的**对话档案**，本模块不持有任何
提示词文本，调用方也无从传入——提示词是后端的领域资产，前端只能点名场景。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from backend.core.contracts import QuestionsConfig, StreamEvent
from backend.core.errors import AppError, ErrorCode
from backend.services.llm_factory import StreamingChatModel, get_llm
from backend.services.prompts import (
    DEFAULT_CONVERSATION_PROFILE,
    ConversationProfile,
    resolve_profile,
)
from backend.services.stream_reply import iter_reply_events

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

# 首轮开场白与系统人设已上收到 `services/prompts.py` 的「对话档案」，
# 由 `resolve_profile()` 按 key 取用——两者必须同口径，不能再分居两处。

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
    system_prompt: str,
    history: Iterable[FrontendMessage | dict[str, Any]] | None = None,
    user_message: str | None = None,
) -> list[BaseMessage]:
    """把系统提示词、历史消息与本轮输入组装成 LangChain 消息列表。

    历史里的 `system` 消息会原样保留（放在系统提示词之后），`user` 转
    `HumanMessage`、`assistant` 转 `AIMessage`，以此保证 user / assistant
    严格交替——Anthropic 对消息顺序有硬性要求。

    Args:
        system_prompt: 系统提示词，由调用方从对话档案里取出（没有默认值，
            避免「忘记传」时静默用上另一个人设）。
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


# ----------------------------------------------------------------------------
# 会话记录
# ----------------------------------------------------------------------------


@dataclass
class _ConversationRecord:
    """内存中的会话记录。当前没有接数据库，进程重启后数据丢失。"""

    id: str
    title: str
    status: str = "active"
    form_data: dict[str, Any] = field(default_factory=dict)
    requirements: str = ""
    messages: list[FrontendMessage] = field(default_factory=list)
    # 只存档案标识，不存解析后的提示词文本：这样以后改提示词，
    # 进行中的会话下一轮就生效，库里也不会留下「冻结的旧人设」。
    profile_key: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ConversationService:
    """会话服务。

    职责：管理会话上下文、把问卷答案整理成模型上下文、调用 LLM 并以
    `StreamEvent` 事件流的形式逐字返回回复。

    提示词不由调用方传入：本服务按 `profile_key` 从 `services/prompts.py` 的
    对话档案注册表里取人设与开场白。路由层不暴露该参数，前端也无从指定。

    Attributes:
        profile_key: 本服务默认使用的对话档案标识。
    """

    def __init__(
        self,
        llm: StreamingChatModel | None = None,
        profile_key: str = DEFAULT_CONVERSATION_PROFILE,
    ) -> None:
        """初始化服务。

        Args:
            llm: 自定义 LLM 实例；不传则在首次使用时通过 `get_llm()` 惰性创建。
            profile_key: 默认对话档案标识。

        Raises:
            KeyError: `profile_key` 未在注册表中登记——在启动阶段就暴露写错的档案名，
                好过某次对话中途静默退回默认人设。
        """
        resolve_profile(profile_key)
        self.profile_key = profile_key
        self._llm = llm
        self._conversations: dict[str, _ConversationRecord] = {}

    # ------------------------------------------------------------------
    # LLM 惰性获取
    # ------------------------------------------------------------------

    @property
    def llm(self) -> StreamingChatModel:
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

    def resolve_request_profile(self, profile_key: str) -> ConversationProfile:
        """解析本次请求要用的对话档案。

        留空时取本服务的默认档案；传了未注册的标识则**当场拒绝**（`INVALID_REQUEST`），
        绝不静默回退到默认人设——那会让「前端点名了 A 场景，实际跑的是 B 场景」这种事
        既没有报错、也没有日志，只表现为回复风格莫名不对。

        Args:
            profile_key: 本次请求指定的档案标识；空串表示用服务默认。

        Returns:
            对应的 `ConversationProfile`。

        Raises:
            AppError: 标识未在注册表中登记。
        """
        try:
            return resolve_profile(profile_key or self.profile_key)
        except KeyError as exc:
            raise AppError(ErrorCode.INVALID_REQUEST, str(exc)) from exc

    @staticmethod
    def _opening_context(
        profile: ConversationProfile, record: _ConversationRecord
    ) -> dict[str, str]:
        """按档案声明的占位符名收集开场白的取值。

        `requirements` 是唯一的**保留名**：它是「问卷答案整理成的需求文本」，由本地
        格式化函数产出，而不是直接取自 `form_data`。其余名字一律按同名键从 `form_data`
        里取（缺失即空串）——因此新增一个档案只需要声明自己要哪些占位符，
        服务层不必认识任何具体档案。

        Args:
            profile: 本次使用的对话档案。
            record: 已登记好的会话记录。

        Returns:
            占位符名 → 文本，可直接 `opening.format(**context)`。
        """
        context: dict[str, str] = {}
        for name in profile.opening_fields:
            if name == "requirements":
                context[name] = record.requirements
            else:
                context[name] = _normalize_value(record.form_data.get(name))
        return context

    def _create_record(
        self,
        form_data: dict[str, Any],
        title: str = "",
        profile_key: str = "",
    ) -> _ConversationRecord:
        """创建并登记一条会话记录。

        Args:
            form_data: 问卷答案。
            title: 会话标题，留空则自动推断。
            profile_key: 本会话使用的对话档案标识；留空取服务默认。
        """
        now = datetime.now()
        record = _ConversationRecord(
            id=self._new_id(),
            title=self._guess_title(form_data, title),
            form_data=dict(form_data),
            requirements=format_form_data(form_data),
            profile_key=profile_key or self.profile_key,
            created_at=now,
            updated_at=now,
        )
        self._conversations[record.id] = record
        return record

    # ------------------------------------------------------------------
    # 流式对话
    # ------------------------------------------------------------------

    async def start_conversation_stream(
        self,
        form_data: dict[str, Any],
        title: str = "",
        profile_key: str = "",
    ) -> AsyncIterator[StreamEvent]:
        """提交表单/文档后开启会话，流式返回模型的第一轮回复。

        流程：校验并解析档案 → 登记会话 → 按档案声明的占位符渲染开场白 →
        让模型先做理解确认与追问 → 逐字推送 → 把这一轮写进会话历史。

        人设与首轮开场白都取自对话档案，调用方**只能点名档案**（`profile_key`），
        不能自带提示词；同一档案内两者同口径，跨档案则共用同一段提问规则。

        Args:
            form_data: 提交上来的表单数据。`clarification` 档案用问卷答案（`QuestionConfig.name`
                作键）；`doc-clarification` 档案用 `document`（文档正文）与 `pending_items`
                （待确认清单文本）。
            title: 会话标题，留空则自动推断。
            profile_key: 对话档案标识；留空取服务默认。

        Yields:
            `StreamEvent`。首个 `status` 事件的 `data` 里带 `conversation_id`，
            前端需要立刻记下来，后续接续对话要靠它。

        Raises:
            AppError: `profile_key` 未注册。
        """
        # 先校验档案再登记会话：未注册的标识当场拒绝，不留下一条用着错人设的会话
        profile: ConversationProfile = self.resolve_request_profile(profile_key)
        record = self._create_record(form_data, title, profile_key=profile.key)
        yield StreamEvent(
            type="status",
            content="已收到你的需求，正在整理分析…",
            data={"conversation_id": record.id},
        )

        # 首轮的用户消息：按档案声明的占位符填充（需求文本 / 文档正文 + 待确认清单）
        first_message = profile.opening.format(**self._opening_context(profile, record))
        messages = _build_lc_messages(
            system_prompt=profile.system_prompt,
            user_message=first_message,
        )

        def _persist(reply: str) -> None:
            """把首轮问答写进历史，供后续接续对话使用。"""
            record.messages.append({"role": "user", "content": first_message})
            record.messages.append({"role": "assistant", "content": reply})
            record.updated_at = datetime.now()

        async for event in iter_reply_events(
            self.llm,
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
        profile_key: str = "",
    ) -> AsyncIterator[StreamEvent]:
        """接续对话，流式返回模型对本轮输入的回复。

        支持两种用法：
        - **有状态**：传 `conversation_id`，历史从服务内存里取（默认）。
        - **无状态**：不传 `conversation_id` 而传 `history`，由前端携带完整历史，
          适合服务重启或多实例部署的场景。

        人设**按档案标识实时解析**，优先取会话记录里的那个（记录标明了这次对话点的是哪个
        场景）；记录已丢失（服务重启 / 无状态模式）时才用请求带来的 `profile_key`，
        两者都没有则回退默认档案。`profile_key` 只是点名场景、不是提示词，因此这不违反
        「提示词全在后端」——否则服务重启后「文档澄清」会悄悄变回「需求澄清」。

        Args:
            user_message: 本轮用户输入。
            conversation_id: 会话 ID；给出时优先使用服务内的历史，并在生成
                结束后把本轮问答写回该会话。
            history: 前端传入的历史消息；传入时优先于服务端历史。
            profile_key: 会话记录缺失时用于解析人设的档案标识。

        Yields:
            `StreamEvent`。`done` 事件的 `data` 带 `conversation_id` 与完整
            `reply`，前端可用它做一次最终对齐。

        Raises:
            AppError: `profile_key` 未注册。
        """
        record = self._conversations.get(conversation_id) if conversation_id else None

        if conversation_id and record is None:
            # ★ 本次改造的核心：这条错误发生在 HTTP 200 的 SSE 流内，前端拿不到
            # 状态码，只能靠 `code` 判断「该不该清掉会话 ID 走无状态重试」。
            # 前端据此分支，而不是匹配 `content` 里的中文文案——文案一改就失效。
            yield StreamEvent(
                type="error",
                content=f"会话不存在或已过期：{conversation_id}",
                data={"conversation_id": conversation_id},
                code=ErrorCode.SESSION_NOT_FOUND.value,
            )
            return

        # 前端显式传入历史时以它为准，否则用服务内存里的历史
        base_history: list[FrontendMessage] = list(history) if history else list(
            record.messages if record else []
        )
        # 有记录就信记录（它标明了这次对话点的是哪个场景），没有才用请求带来的标识
        profile: ConversationProfile = self.resolve_request_profile(
            record.profile_key if record else profile_key
        )
        messages = _build_lc_messages(
            system_prompt=profile.system_prompt,
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

        async for event in iter_reply_events(
            self.llm,
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


def check_profile_key(profile_key: str) -> None:
    """校验请求要用的对话档案；未注册的标识抛 `AppError(INVALID_REQUEST)`。

    路由必须在**返回 SSE 响应之前**调用它：只有错误发生在流开始之前，才可能是一个真正
    的 HTTP 400。等流内再报，前端拿到的是 200 加一条 `error` 事件，状态码就失去意义了。

    Args:
        profile_key: 请求带来的档案标识；空串表示用默认档案。

    Raises:
        AppError: 标识未在注册表中登记。
    """
    get_service().resolve_request_profile(profile_key)


async def start_stream(
    form_data: dict[str, Any],
    title: str = "",
    profile_key: str = "",
) -> AsyncIterator[StreamEvent]:
    """提交表单数据、创建会话，并以事件流形式产出模型的第一轮回复。

    Args:
        form_data: 提交上来的表单数据（含 `doc-clarification` 档案需要的文档正文）。
        title: 会话标题，留空则自动推断。
        profile_key: 对话档案标识；留空取默认（需求澄清）。

    Yields:
        `StreamEvent`，首个 `status` 事件的 `data` 里带 `conversation_id`。
    """
    async for event in get_service().start_conversation_stream(
        form_data=form_data,
        title=title,
        profile_key=profile_key,
    ):
        yield event


async def continue_stream(
    user_message: str,
    conversation_id: str | None = None,
    history: list[FrontendMessage] | None = None,
    profile_key: str = "",
) -> AsyncIterator[StreamEvent]:
    """接续对话，以事件流形式产出模型对本轮输入的回复。

    Args:
        user_message: 本轮用户输入。
        conversation_id: 会话 ID；为 `None` 时走无状态模式。
        history: 前端回传的历史消息；传入时优先于服务端保存的历史。
        profile_key: 会话记录缺失时用于解析人设的档案标识。

    Yields:
        `StreamEvent`，`done` 事件的 `data` 带 `conversation_id` 与完整 `reply`。
    """
    async for event in get_service().continue_conversation_stream(
        user_message=user_message,
        conversation_id=conversation_id,
        history=history,
        profile_key=profile_key,
    ):
        yield event
