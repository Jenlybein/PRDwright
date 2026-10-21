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
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Literal, NotRequired, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from backend.core.contracts import QuestionsConfig, StreamEvent
from backend.core.errors import AppError, ErrorCode
from backend.services.clarification_ask import asks_from_tool_calls, build_ask_tool
from backend.services.llm_factory import StreamingChatModel, config_fingerprint, get_llm
from backend.services.prompts import (
    CLARIFICATION_ASK_REMINDER,
    DEFAULT_CONVERSATION_PROFILE,
    DOC_CLARIFICATION_PROFILE_KEY,
    ConversationProfile,
    resolve_profile,
)
from backend.services.stream_reply import iter_reply_events, iter_reply_events_with_tools

logger = logging.getLogger(__name__)

# 问卷配置文件：backend/src/backend/core/question_config.json
QUESTION_CONFIG_PATH = Path(__file__).resolve().parents[1] / "core" / "question_config.json"

# ----------------------------------------------------------------------------
# 前端消息格式
# ----------------------------------------------------------------------------

# 角色取值，与前端聊天列表保持一致。`tool` 是工具调用链的一环：助手提问走
# `tool_calls`，用户作答回灌走 `tool` 角色（对齐 Claude Code / Codex）。
MessageRole = Literal["system", "user", "assistant", "tool"]


class FrontendMessage(TypedDict):
    """前端聊天消息结构。

    Attributes:
        role: 消息角色，`system` / `user` / `assistant` / `tool`。
        content: 消息正文。
        tool_calls: assistant 消息发起的工具调用（`[{id, name, args}]`），作答回灌时
            模型要看到「先调用、后应答」的成对结构。
        tool_call_id: `tool` 消息应答的工具调用 id。
        tool_result: `tool` 消息携带的结构化答案（`{tool_call_id, answers}`），
            有它时模型侧正文由它重新组装，`content` 只作展示文本。
    """

    role: MessageRole
    content: str
    tool_calls: NotRequired[list[dict[str, Any]]]
    tool_call_id: NotRequired[str]
    tool_result: NotRequired[dict[str, Any]]


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
    "tool": "tool",
    "function": "tool",
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
    追加在末尾，直接用字段名展示，避免答案被静默丢弃。空值题目不进已答列表。

    **未填的题目会在末尾汇总成一行**（`以下题目用户未填写：…`）。这一行不是装饰：
    没有它，模型只看到若干个「有答案的题」，无从判断某道题是「被问过、用户留空」还是
    「压根不存在这道题」——后者会诱使它自己编一个答案补上。只列已答项等于把留空
    当成了「没这回事」，而两者的处理方式正好相反（一个是该追问，一个是不该提）。

    Args:
        form_data: 问卷答案，key 为 `QuestionConfig.name`，value 为任意类型。
        questions: 问卷配置；不传则自动读取配置文件。

    Returns:
        Markdown 无序列表文本，每形如 `- **题目文案**：答案`；末尾按需附未填题目汇总。
    """
    if not form_data:
        return "（用户尚未填写任何需求信息）"

    if questions is None:
        questions = load_questions_config()
        if questions is None:
            logger.warning(
                "问卷配置未加载，本次无法汇总「未填写」的题目",
                extra={"event": "requirements.config_missing"},
            )

    # 建立 name → label 映射，并保留配置里的题目顺序
    ordered: list[tuple[str, str]] = []
    if questions is not None:
        for question in [*questions.base_questions, *questions.advanced_questions]:
            ordered.append((question.name, question.label))

    lines: list[str] = []
    handled: set[str] = set()
    # 配置里声明过、但用户留空的题目（按配置顺序，便于与表单逐项对照）
    unfilled: list[str] = []

    for name, label in ordered:
        handled.add(name)
        text = _normalize_value(form_data.get(name))
        if text:
            lines.append(f"- **{label}**：{text}")
        else:
            unfilled.append(label)

    # 配置之外的字段（例如前端后续新增的题目）
    for key, value in form_data.items():
        if key in handled:
            continue
        text = _normalize_value(value)
        if text:
            lines.append(f"- **{key}**：{text}")

    if unfilled:
        lines.append(f"以下题目用户未填写：{'、'.join(unfilled)}")

    return "\n".join(lines) if lines else "（用户尚未填写任何需求信息）"


def _tool_result_content(tool_result: dict[str, Any] | None, fallback: str = "") -> str:
    """把结构化作答渲染成工具结果正文。

    每条答案都带 `question` / `texts`（自解释冗余），模型不必回头翻自己的工具调用
    参数就能看懂选了什么——对齐 Claude Code `AskUserQuestion` 的结果形状。
    没有结构化答案时退化成展示文本，再没有就给一句占位，绝不输出空正文。

    Args:
        tool_result: 结构化作答（`{tool_call_id, answers}`）。
        fallback: 没有可渲染答案时的兜底文本（通常是给人看的展示文本）。

    Returns:
        `ToolMessage` 的正文。
    """
    answers = (tool_result or {}).get("answers")
    if isinstance(answers, list) and answers:
        return json.dumps(answers, ensure_ascii=False)
    return fallback or "（用户未选择任何选项）"


def _build_lc_messages(
    system_prompt: str,
    history: Iterable[FrontendMessage | dict[str, Any]] | None = None,
    user_message: str | None = None,
    tool_result: dict[str, Any] | None = None,
) -> list[BaseMessage]:
    """把系统提示词、历史消息与本轮输入组装成 LangChain 消息列表。

    历史里的 `system` 消息会原样保留（放在系统提示词之后），`user` 转
    `HumanMessage`、`assistant` 转 `AIMessage`，以此保证 user / assistant
    严格交替——Anthropic 对消息顺序有硬性要求。

    工具调用链（对齐 Claude Code / Codex）：带 `tool_calls` 的 assistant 消息原样
    保留，`tool` 角色的消息转 `ToolMessage` 应答之；**本轮带 `tool_result` 时不再
    投喂 `HumanMessage`**，答案只走工具结果回去。用户没点卡片、直接用自由文本作答时，
    那段文本会被包成 `ToolMessage` 回灌给悬空的工具调用——否则历史里留下「调了工具
    却没人应答」的结构，上游 API 直接报错。

    Args:
        system_prompt: 系统提示词，由调用方从对话档案里取出（没有默认值，
            避免「忘记传」时静默用上另一个人设）。
        history: 历史消息，元素为 `FrontendMessage` 或等价字典。
        user_message: 本轮用户输入；为空则不追加。
        tool_result: 本轮的结构化作答（`{tool_call_id, answers}`）；带上时按
            工具结果回灌，与 `user_message` 互斥使用（展示文本走 `user_message`
            仅作兜底渲染）。

    Returns:
        LangChain 消息列表，首条固定为 `SystemMessage`。
    """
    messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]
    # 悬空的工具调用 → 工具应答应插入的下标（紧贴发起调用的 assistant 之后）。
    # 记下标而不是追加到末尾：格式重申等系统消息可能已经排在后面，而上游要求
    # ToolMessage 与发起调用的 assistant 相邻成对。
    pending: dict[str, int] = {}

    def _answer(call_id: str, body: str) -> None:
        """把 `body` 作为 `call_id` 的工具应答插进消息列表，并维护剩余下标。"""
        at = pending.pop(call_id, len(messages))
        messages.insert(at, ToolMessage(content=body, tool_call_id=call_id))
        for cid in list(pending):
            if pending[cid] >= at:
                pending[cid] += 1

    for item in history or []:
        role = _normalize_role(item.get("role"))
        content = str(item.get("content", "") or "")
        tool_calls = [c for c in (item.get("tool_calls") or []) if isinstance(c, dict)]
        tool_call_id = str(item.get("tool_call_id") or "")
        tool_result_item = item.get("tool_result")

        if role == "assistant":
            # 有工具调用时正文可以为空——整条消息照样要发出去，否则应答配不上对
            if tool_calls:
                messages.append(AIMessage(content=content, tool_calls=tool_calls))
                for call in tool_calls:
                    call_id = str(call.get("id") or "")
                    if call_id:
                        pending[call_id] = len(messages)
            elif content:
                messages.append(AIMessage(content=content))
            continue

        if role == "tool":
            body = _tool_result_content(
                tool_result_item if isinstance(tool_result_item, dict) else None,
                content,
            )
            if tool_call_id in pending:
                _answer(tool_call_id, body)
            elif body:
                # 配不上的工具应答（重复应答 / 坏 id）：降级成普通用户文本保住内容，
                # 绝不发一条没有对应 tool_call 的 ToolMessage
                messages.append(HumanMessage(content=body))
            continue

        if not content:
            continue

        if role == "user":
            if pending:
                # 自由文本落在有待应答工具调用的位置上：它就是对那些问题的回答
                # （用户没点卡片、直接在输入框里写的），包成工具结果回灌。
                # 不这么做的下场是历史里留下「调了工具却没人应答」的结构，
                # 上游 API 直接报错——这不是风格问题，是能不能跑的问题。
                for cid in list(pending):
                    _answer(cid, content)
            else:
                messages.append(HumanMessage(content=content))
            continue

        # system 等其余角色原样保留，**不**参与工具应答的配对
        messages.append(SystemMessage(content=content))

    # 本轮输入：带结构化作答就按工具结果回灌（不再重复投喂人话）；
    # 配不上任何悬空调用时（旧快照没存 tool_calls 等）降级成普通用户消息，保住展示文本。
    if tool_result is not None:
        body = _tool_result_content(tool_result, user_message or "")
        call_id = str(tool_result.get("tool_call_id") or "")
        matched = call_id in pending
        if matched:
            _answer(call_id, body)
        # 并行调用留下的其余悬空调用：用展示文本作答（结构化正文只归它自己的 call_id）
        for cid in list(pending):
            _answer(cid, user_message or body)
        if not matched:
            text = user_message or body
            last = messages[-1] if len(messages) > 1 else None
            if not (isinstance(last, HumanMessage) and last.content == text):
                messages.append(HumanMessage(content=text))
        return messages

    if user_message:
        if pending:
            # 自由文本作答悬空的工具调用（与历史分支同一规则）
            for cid in list(pending):
                _answer(cid, user_message)
            return messages
        last = messages[-1] if len(messages) > 1 else None
        if not (isinstance(last, HumanMessage) and last.content == user_message):
            messages.append(HumanMessage(content=user_message))

    return messages


def _reminded_history(
    history: Iterable[FrontendMessage | dict[str, Any]],
    profile: ConversationProfile,
) -> list[FrontendMessage | dict[str, Any]]:
    """续聊轮次在历史末尾追加一条「格式重申」系统消息（仅本轮发送，不落库）。

    长对话里模型会模仿自己历史中「只写 A/B/C 文字、不附块」的写法，把结构化块丢掉，
    导致前端选项卡弹不出来。把重申作为离当前最近的一条系统消息补到末尾，专门点破这个退化点。
    只在澄清类档案上注入；返回副本，绝不改动调用方的历史（避免被持久化或随前端历史回传累积）。
    """
    if profile.key not in (DEFAULT_CONVERSATION_PROFILE, DOC_CLARIFICATION_PROFILE_KEY):
        return list(history)
    reminded: list[FrontendMessage | dict[str, Any]] = list(history)
    reminded.append({"role": "system", "content": CLARIFICATION_ASK_REMINDER})
    return reminded


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
        # 注入的实例（测试）永远原样复用；未注入的按配置指纹重建。
        self._llm_injected = llm is not None
        self._llm_key: tuple[str, str, str, int] | None = None
        self._conversations: dict[str, _ConversationRecord] = {}

    # ------------------------------------------------------------------
    # LLM 惰性获取
    # ------------------------------------------------------------------

    @property
    def llm(self) -> StreamingChatModel:
        """LLM 实例；按配置指纹惰性创建。

        ⚠️ 这个属性以前是**永久缓存**的，而 `get_service()` 又是模块级单例——
        两者叠加的后果是「后台换了供应商，对话链路永远用旧的那家」：
        线程里的历史消息照样能发出去，只是发给了上一家的地址。按指纹重建即可。
        """
        if self._llm_injected:
            return self._llm  # type: ignore[return-value]
        key = config_fingerprint()
        if self._llm is None or self._llm_key != key:
            self._llm = get_llm()
            self._llm_key = key
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

    async def _stream_with_asks(
        self,
        messages: list[BaseMessage],
        *,
        status_text: str | None = None,
        extra_data: dict[str, Any] | None = None,
        on_persist: Callable[[str, list[dict[str, Any]]], None] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式产出回复，并把工具调用里的结构化选择题解析进 `done`。

        提问走**原生工具调用**：绑定 `ask_clarification_questions` 工具，模型用工具调用
        输出结构化题目（正文天然干净、不藏哨兵）。收尾时从累积的 `tool_calls` 里取出题目、
        校验、分配 id/label，随 `done` 下发。

        ⚠️ `on_persist` 收到的是 `(干净正文, tool_calls)`。正文不含任何标记块；`tool_calls`
        需要一并落库（下一轮作答回灌要用 `tool_call_id` 对回这次调用），但**只在作答侧
        （Step 2）**才真正进会话历史——本阶段先把它交出去，暂存与否由调用方决定。

        Args:
            messages: 已组装好的 LangChain 消息。
            status_text: 首个状态事件的提示语；为 `None` 时不发（调用方已发过）。
            extra_data: 附加到 `done` 事件的数据（如会话 ID）。
            on_persist: 生成成功后的回调，接收 `(干净正文, tool_calls)`。
        """
        bound = self.llm.bind_tools(
            [build_ask_tool()], tool_choice="auto", parallel_tool_calls=False
        )
        async for event in iter_reply_events_with_tools(
            bound,
            messages,
            status_text=status_text,
            extra_data=extra_data,
        ):
            if event.type == "done":
                reply = str(event.data.get("reply", ""))
                tool_calls = list(event.data.get("tool_calls", []) or [])
                asks = asks_from_tool_calls(tool_calls)
                # 每轮必打的汇总信号：无论哪类失败，都能从这一行看出「调没调工具、出了几题」。
                # `tool_called=false` + `asks_count=0` 即「模型没调工具」（配合 ask.no_tool_call）；
                # `tool_called=true` + `asks_count=0` 即「调了但参数全被丢弃」（配合 ask.bad_args / too_few_options）。
                logger.info(
                    "澄清选择题解析结果",
                    extra={
                        "event": "ask.result",
                        "tool_called": bool(tool_calls),
                        "asks_count": len(asks),
                    },
                )
                logger.debug(
                    "澄清回复工具调用",
                    extra={"event": "ask.raw_reply", "tool_calls": tool_calls},
                )
                if on_persist is not None:
                    on_persist(reply, tool_calls)
                done_data: dict[str, Any] = {
                    **(event.data or {}),
                    "reply": reply,
                    # 结构化选择题：前端据此把选项渲染成可点的卡片。
                    # 没有工具调用时是空数组——「没调工具」是常态，不是异常。
                    "clarification_asks": asks,
                }
                # 顶层 tool_call_id：供作答回灌定位「答案该回给哪次调用」。
                # parallel_tool_calls=False 下一轮至多一次调用；真出现多次时取第一个带 id 的。
                # 拿不到 id 就不带这个键（前端据此降级成纯文本作答），不下发空串。
                for call in tool_calls:
                    if isinstance(call, dict) and call.get("id"):
                        done_data["tool_call_id"] = str(call["id"])
                        break
                yield StreamEvent(
                    type="done",
                    content="",
                    data=done_data,
                )
            else:
                # status / error 原样透传。注意出错时上游**不发 done**，
                # 因此 on_persist 的时机与原来的 `on_finish` 完全一致（只在成功时）。
                yield event

    async def start_conversation_stream(
        self,
        form_data: dict[str, Any],
        title: str = "",
        profile_key: str = "",
        on_opening: Callable[[str], None] | None = None,
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
            on_opening: 拿到**渲染后的开场白**时回调一次（同步、在首个事件之前）。

                为什么要这个钩子：开场白（表单答案 / 文档正文 + 待确认清单）是
                **喂给模型却不展示给用户**的那部分上下文。调用方把它存成一条
                `system` 轮次之后，后续轮次就能从库里重放出完整上下文，
                而不必依赖服务内存里的会话记录——进程一重启，记录就没了。
                放在这里回调而不是让调用方自己渲染：开场白的模板由档案持有，
                在外面再拼一次等于把同一套措辞维护两份。

        Yields:
            `StreamEvent`。首个 `status` 事件的 `data` 里带 `conversation_id`，
            前端需要立刻记下来，后续接续对话要靠它。

        Raises:
            AppError: `profile_key` 未注册。
        """
        # 先校验档案再登记会话：未注册的标识当场拒绝，不留下一条用着错人设的会话
        profile: ConversationProfile = self.resolve_request_profile(profile_key)
        record = self._create_record(form_data, title, profile_key=profile.key)
        # 状态文案跟档案走：文档澄清时说「已收到你的需求」会让人以为在讨论新需求。
        status_text = (
            "已读取文档，正在梳理待确认项…"
            if profile.key == DOC_CLARIFICATION_PROFILE_KEY
            else "已收到你的需求，正在整理分析…"
        )
        yield StreamEvent(
            type="status",
            content=status_text,
            data={"conversation_id": record.id},
        )

        # 首轮的用户消息：按档案声明的占位符填充（需求文本 / 文档正文 + 待确认清单）
        first_message = profile.opening.format(**self._opening_context(profile, record))
        if on_opening is not None:
            # 在首个事件之前回调：调用方要把它写进轮次表，而轮次的顺序由 id 决定，
            # 晚于 assistant 落库就会排到它后面，下一轮拼历史时上下文顺序就错了。
            on_opening(first_message)
        messages = _build_lc_messages(
            system_prompt=profile.system_prompt,
            user_message=first_message,
        )

        def _persist(reply: str, tool_calls: list[dict[str, Any]] | None = None) -> None:
            """把首轮问答写进历史，供后续接续对话使用。

            `tool_calls` 一并落库：作答轮要把答案作为 ToolMessage 对回这次调用，
            历史里必须留着调用本身（`tool_call_id` 的锚点）。
            """
            record.messages.append({"role": "user", "content": first_message})
            assistant: dict[str, Any] = {"role": "assistant", "content": reply}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            record.messages.append(assistant)
            record.updated_at = datetime.now()

        # `_persist` 收到的是干净正文 + 本轮 tool_calls（见 `_stream_with_asks`）
        async for event in self._stream_with_asks(
            messages,
            extra_data={"conversation_id": record.id},
            on_persist=_persist,
        ):
            yield event

    async def continue_conversation_stream(
        self,
        user_message: str,
        conversation_id: str | None = None,
        history: list[FrontendMessage] | None = None,
        profile_key: str = "",
        tool_result: dict[str, Any] | None = None,
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
            user_message: 本轮用户输入。带 `tool_result` 时它退化为展示文本
                （答案本体走工具结果，不重复投喂人话）。
            conversation_id: 会话 ID；给出时优先使用服务内的历史，并在生成
                结束后把本轮问答写回该会话。
            history: 前端传入的历史消息；传入时优先于服务端历史。
            profile_key: 会话记录缺失时用于解析人设的档案标识。
            tool_result: 本轮的结构化作答（`{tool_call_id, answers}`）；带上时
                按 ToolMessage 回灌给发起提问的那次工具调用。

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
            history=_reminded_history(base_history, profile),
            user_message=user_message,
            tool_result=tool_result,
        )

        def _persist(reply: str, tool_calls: list[dict[str, Any]] | None = None) -> None:
            """把本轮问答写回会话历史。

            带 `tool_result` 的作答轮：把工具应答插到发起调用的 assistant 之后
            （成对相邻），**不再另记一条 user 消息**——答案只走 ToolMessage，避免
            同一份作答在历史里出现两次。纯文本轮照旧记 user + assistant。
            """
            target: list[Any] = record.messages if record is not None else base_history
            if tool_result is not None:
                call_id = str(tool_result.get("tool_call_id") or "")
                body = _tool_result_content(tool_result, user_message)
                entry: dict[str, Any] = {
                    "role": "tool",
                    "content": user_message or body,
                    "tool_result": dict(tool_result),
                }
                if call_id:
                    entry["tool_call_id"] = call_id
                # 从后往前找发起这次调用的 assistant，把应答插到它后面；
                # 对不上（历史里没有这次调用）就退化成普通用户消息，绝不留
                # 「有 ToolMessage 却没有对应 tool_call」的非法结构。
                insert_at = -1
                if call_id:
                    for index in range(len(target) - 1, -1, -1):
                        item = target[index]
                        if not isinstance(item, dict):
                            continue
                        if item.get("role") != "assistant":
                            continue
                        calls = item.get("tool_calls") or []
                        if any(
                            isinstance(call, dict) and call.get("id") == call_id
                            for call in calls
                        ):
                            insert_at = index + 1
                            break
                if insert_at >= 0:
                    target.insert(insert_at, entry)
                else:
                    target.append({"role": "user", "content": user_message or body})
            else:
                target.append({"role": "user", "content": user_message})
            assistant: dict[str, Any] = {"role": "assistant", "content": reply}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            target.append(assistant)
            if record is not None:
                record.updated_at = datetime.now()

        async for event in self._stream_with_asks(
            messages,
            status_text="正在思考…",
            extra_data={"conversation_id": conversation_id or ""},
            on_persist=_persist,
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
    tool_result: dict[str, Any] | None = None,
) -> AsyncIterator[StreamEvent]:
    """接续对话，以事件流形式产出模型对本轮输入的回复。

    Args:
        user_message: 本轮用户输入。
        conversation_id: 会话 ID；为 `None` 时走无状态模式。
        history: 前端回传的历史消息；传入时优先于服务端保存的历史。
        profile_key: 会话记录缺失时用于解析人设的档案标识。
        tool_result: 本轮的结构化作答（`{tool_call_id, answers}`）；带上时
            按 ToolMessage 回灌，不重复投喂普通用户消息。

    Yields:
        `StreamEvent`，`done` 事件的 `data` 带 `conversation_id` 与完整 `reply`。
    """
    async for event in get_service().continue_conversation_stream(
        user_message=user_message,
        conversation_id=conversation_id,
        history=history,
        profile_key=profile_key,
        tool_result=tool_result,
    ):
        yield event
