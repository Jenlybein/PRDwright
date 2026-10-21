"""文档业务服务。

负责把对话澄清结果整理成可落地的文档：PRD / 接口文档 / 提示词套件，并支持
对已有文档做优化改写。所有生成都走 LLM 的 `astream` 流式接口，以
`StreamEvent` 事件流的形式逐字推送，与 `conversation_service` 的流式约定一致。

对外暴露三类接口：
- **流式生成**（`*_stream`，供路由层 `StreamingResponse` 消费）：
  - `generate_prd_stream`：根据对话历史 + 问卷答案生成 PRD。
  - `generate_api_docs_stream`：根据 PRD 正文生成接口文档（团队规范由技能包全量注入）。
  - `generate_prompts_stream`：根据 PRD 正文生成提示词套件（产出要求由技能包提供）。
  - `optimize_document_stream`：按用户指令优化既有文档。
- **结构化摘要回填**（`sync_summary_from_conversation`）：从新增对话里抽取用户**明确补充
  或修正**的字段，回填成一份完整摘要，供前端做 Diff 与确认。

提示词模板集中在 `services/prompts.py`，本模块只负责「加载模板 + 拼接变量
+ 调用模型流式生成」，不维护大段提示词文本。

PRD / 接口文档 / 提示词套件三类生成均走技能包驱动：各自从对应技能包
（`skills/prd-generator`、`skills/api-docs-generator`、`skills/prompts-generator`）取用
字段契约、团队规范或产出要求，拼进提示词；技能包缺失时自动降级为 `prompts.py` 里的
内置提示词。这一改动废弃了原先的 RAG 检索（见 ADR-020）：团队规范不再「按 PRD 关键词
挑几条」下发，而是整段注入，避免「检索没命中就悄悄丢规范」的静默降级。

按摘要生成 PRD 时优先走**分章节生成 + 写作/审核双 Agent**（`services/prd_section_writer.py`），
只有技能包缺失时才降级为整篇一次性生成。

模块划分（避免循环依赖）：
- `llm_utils`：模型输出 → 纯文本 / JSON 的通用处理；
- `prd_skill`：技能包资源定位与输入材料格式化；
- `prd_section_writer`：分章节生成器（依赖上面两个）。
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, AsyncIterator

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.core.config import get_settings
from backend.core.contracts import StreamEvent
from backend.services.doc_skeleton import extract_headings, skeletonize
from backend.services.llm_factory import StreamingChatModel, get_llm
from backend.services.llm_utils import extract_json_object
from backend.services.document_review import (
    ReviewOutcome,
    stream_review_and_rewrite,
)
from backend.services.pending_items import (
    PendingBlockSuppressor,
    extract,
    format_pending_items,
    render_spec,
)
from backend.services.prd_section_writer import PrdSectionWriter, is_available as prd_sections_available
from backend.services.prd_skill import (
    build_prd_system_prompt,
    format_conversation,
    format_freeform_requirements,
    format_summary_as_requirements,
    load_prd_skill_artifacts,
)
from backend.services.prompts import (
    API_DOCS_GENERATION_PROMPT,
    AUTO_CLARIFY_PROMPT,
    CLARIFICATION_REWRITE_PROMPT,
    NO_API_DOCS_NOTE,
    OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE,
    PRD_GENERATION_PROMPT,
    PRD_SKILL_GENERATION_PROMPT,
    PROMPTS_GENERATION_PROMPT,
    SUMMARY_SYNC_PROMPT_TEMPLATE,
    SUMMARY_SYNC_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)
from backend.services.skill_registry import load_skill_artifacts
from backend.services.stream_reply import collect_reply, iter_reply_events

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# 结构化摘要回填：候选值 → 与原始摘要合并
# ----------------------------------------------------------------------------


def _is_blank(value: Any) -> bool:
    """判断候选值是否「等于没说」：None / 空白串 / 空数组 / 空对象。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


# 结构化集合里允许「原项没填、但前端表单有对应输入框」的子字段。
# 原项缺了这些键时准许模型补上——否则用户说「这个功能的价值是省时间」这类补充
# 会被规则 3 一并丢掉。不在此表里的键仍然一律丢弃（不凭空新增字段）。
LIST_ITEM_KEYS: dict[str, frozenset[str]] = {
    "mvp_features": frozenset({"name", "description", "user_value"}),
    "ui_pages": frozenset({"name", "modules", "notes"}),
}


def _merge_list_item(template_items: list[Any], item: Any, field: str | None = None) -> Any:
    """合并数组里的单个元素。

    对象元素按 `name` 对齐到旧元素做**字段级**合并（模型只回传 `name` 时不会把
    原描述冲掉）；对不上的名称视为用户明确新增的项，保留但只取「旧元素出现过的键 ∪
    `LIST_ITEM_KEYS` 里登记的键」，避免引入摘要里没有、前端也编辑不了的新字段。

    Args:
        template_items: 原始数组，用于按名称对齐与推断允许的键。
        item: 候选数组里的一个元素。
        field: 该数组所属的顶层字段名，用于查 `LIST_ITEM_KEYS`。

    Returns:
        合并后的元素；无法识别时返回 `None`（该元素被丢弃）。
    """
    if isinstance(item, dict):
        extra_keys = LIST_ITEM_KEYS.get(field or "", frozenset())
        name = item.get("name")
        template = next(
            (old for old in template_items if isinstance(old, dict) and old.get("name") == name),
            None,
        )
        if template is None:
            allowed = {key for old in template_items if isinstance(old, dict) for key in old} | extra_keys
            pruned = {key: value for key, value in item.items() if key in allowed} if allowed else dict(item)
            return pruned or None

        merged: dict[str, Any] = {}
        for key in list(template.keys()) + [k for k in extra_keys if k not in template]:
            if key not in item:
                if key in template:
                    merged[key] = template[key]
                continue
            if key in template:
                value = _merge_value(template[key], item[key], field=key)
                merged[key] = template[key] if value is None else value
            elif not _is_blank(item[key]):
                # 原项没填、但表单能编辑的子字段：用户明确给了就补上
                merged[key] = item[key]
        return merged

    if isinstance(item, str):
        return item.strip() or None
    if isinstance(item, (int, float, bool)):
        return item
    return None


def _merge_value(old: Any, new: Any, field: str | None = None) -> Any:
    """把候选值合并到原值上。

    Args:
        old: 原始值。
        new: 候选值。
        field: 当前所处的顶层字段名，用于在数组项里判断允许补全的子字段。

    Returns:
        合并后的值；返回 `None` 表示「保持原值」（候选为空、类型不合法或无从判断）。
    """
    # 规则 2：不确定 / 没说的内容保持原值
    if _is_blank(new):
        return None

    if isinstance(old, list):
        if isinstance(new, list):
            merged = [_merge_list_item(old, item, field) for item in new]
            kept = [item for item in merged if item is not None]
            return kept or None
        # 字符串数组允许被「单个字符串」替换（schema 允许数组字段传字符串）
        if isinstance(new, str) and all(isinstance(item, str) for item in old):
            return [new.strip()]
        return None

    if isinstance(old, dict):
        if not isinstance(new, dict):
            return None
        # 规则 3：只认原结构里已有的键，候选里多出来的键直接丢弃
        merged_dict: dict[str, Any] = {}
        for key, old_value in old.items():
            if key not in new:
                merged_dict[key] = old_value
                continue
            value = _merge_value(old_value, new[key], field=key)
            merged_dict[key] = old_value if value is None else value
        return merged_dict or None

    if isinstance(new, str):
        return new.strip()
    if isinstance(new, (int, float, bool)):
        return new
    return None


def _merge_summary(original: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """把模型给出的候选摘要合并回原始摘要，产出完整的新摘要。

    这是四条回填规则的最后一道闸门：即使模型不守规矩，也不会新增字段、不会把
    已有内容静默清空。规则如下：

    1. 只接受原始摘要里**已存在**的顶层键，候选里多出来的键一律丢弃。
    2. 候选里缺失该键、或候选值为空（`None` / 空串 / 空数组 / 空对象）时，保留原值。
    3. 数组以候选为准（候选数组为空则保留原数组）；对象元素按 `name` 对齐做字段级合并，
       并允许补全 `LIST_ITEM_KEYS` 里登记的子字段。
    4. 只有非空、类型合法的候选值才会覆盖原值。

    Args:
        original: 原始结构化需求摘要。
        candidate: 模型返回的候选摘要。

    Returns:
        合并后的完整摘要（深拷贝，不修改入参）。
    """
    result = copy.deepcopy(original)
    for key, old_value in original.items():
        if key not in candidate:
            continue
        merged = _merge_value(old_value, candidate[key], field=key)
        if merged is not None:
            result[key] = merged
    return result


# 改写既有文档（optimize / clarify）时注入哪些技能资源才算「产出规范」。
#
# 这份白名单本身就是取舍的落点：**不整包注入**。例如 PRD 的 `prd_template` 是给「从零生成」
# 用的骨架，里面带 `{{字段名}}` 占位符；把它塞进「改写既有文档」的提示词里，只会诱导模型
# 重新渲染一遍占位符，反而破坏原文。字段契约同理——改写不需要它。
_REWRITE_REFERENCE_KEYS: dict[str, tuple[str, ...]] = {
    "prd": ("writing_rules",),
    "api_docs": ("api_specs",),
    "prompts": ("prompts_guide",),
}

# 资源语义键 → 中文小标题（纯展示；没登记过的键直接用原文键名）
_SKILL_KEY_LABELS: dict[str, str] = {
    "writing_rules": "写作规范",
    "prd_template": "PRD 模板",
    "field_schema": "输入字段契约",
    "api_specs": "团队接口规范",
    "prompts_guide": "提示词套件产出要求",
}


def review_enabled_for_whole_document(value: bool | None) -> bool:
    """整篇类产物（接口文档 / 提示词套件）本次是否跑「审一次 → 必要时改写一次」。

    与 PRD 的逐章审核共用一个请求字段 `review_enabled`，但**默认口径不同**：
    `None` 在这里视为**开启**——这两份产物没有独立的配置开关，历史行为就是审，
    只有快捷流程会在请求体里显式传 `False`（ADR-034：快捷流程不审不改）。

    写成「只有 `False` 才是关」而不是「只有 `True` 才是开」：未知/缺省值一律保持
    原有行为，别因为一个前端漏传字段就悄悄改变产出的质量口径。
    """
    return value is not False


def format_rewrite_reference(kind: str) -> str:
    """按目标产物取「改写时要对齐的规范」文本（整段注入，见 ADR-020）。

    技能包不在位或资源缺失时返回一句**显式**的降级说明，而不是空串：提示词里什么都不写，
    模型会以为「没有规范要守」，而实际约定是被悄悄丢掉了——这类静默降级正是 ADR-020 要消灭的。

    Args:
        kind: 目标产物类型（`prd` / `api_docs` / `prompts`）。空串或未知取值一律降级。

    Returns:
        可直接拼进提示词的段落文本。
    """
    keys = _REWRITE_REFERENCE_KEYS.get(kind, ())
    artifacts = (load_skill_artifacts(kind) or {}) if keys else {}
    blocks = [
        f"### {_SKILL_KEY_LABELS.get(key, key)}\n\n{artifacts[key]}"
        for key in keys
        if artifacts.get(key)
    ]
    if not blocks:
        logger.warning(
            "改写时未加载到 %s 的技能包规范，本次按原文风格保持一致",
            kind or "（未知产物）",
            extra={"event": "rewrite.skill_missing", "kind": kind},
        )
        return "（本次未加载产出规范，请严格保持原文的术语、结构与风格。）"
    return "\n\n".join(blocks)


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
        # 显式注入的实例必须**原样复用**：测试靠注入假模型来数调用次数、按系统提示词
        # 分流（如 `TwoStageLLM`），若在它之外再偷偷建一个「审核专用」实例，
        # 那些断言会全部落空。
        self._llm_injected = llm is not None
        # 分章节生成器。惰性创建：它自己也会惰性获取 LLM，且只有在技能包可用时才会用到。
        self._section_writer: PrdSectionWriter | None = None
        # 审核专用的小输出窗口实例（惰性，见 `review_llm`）
        self._review_llm: StreamingChatModel | None = None

    # ------------------------------------------------------------------
    # LLM 惰性获取
    # ------------------------------------------------------------------

    @property
    def llm(self) -> StreamingChatModel:
        """LLM 实例，首次访问时才创建（避免服务初始化阶段就要求密钥）。"""
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    @property
    def review_llm(self) -> StreamingChatModel:
        """审核专用的**小输出窗口**实例。

        审核只是「判定 + 批注」，输出量与正文不在一个量级，却默认占着生成用的那个
        巨大上限（`ARK_MAX_TOKENS` 实测是 131072）。给它一个远小的窗口既省时间也
        省 token——顺带堵住「模型在审核输出里复述一大段正文」这条浪费路径。

        ⚠️ 为什么只能是另一个实例而不是调用参数：`VolcanoArkChatModel._astream` 读的
        是 `self.max_tokens`，`astream(..., max_tokens=...)` 不报错也不生效。

        ⚠️ 为什么注入了 `llm` 就直接复用它：见 `__init__` 里的说明。

        不需要全局缓存：该封装每次调用都现建现关客户端，实例不持有连接池，
        多一个实例的代价可忽略；这里缓存的粒度是「每个 service 实例」，
        一份文档的两三次审核共用同一个。
        """
        if self._review_llm is None:
            self._review_llm = (
                self.llm
                if self._llm_injected
                else get_llm(max_tokens=get_settings().doc_review_max_tokens)
            )
        return self._review_llm

    def _get_section_writer(self, review_enabled: bool | None = None) -> PrdSectionWriter:
        """获取 PRD 分章节生成器（惰性创建，与本源共用同一个 LLM 实例）。

        Args:
            review_enabled: 本次是否启用逐章审核/改写。**不传（`None`）返回共享实例**，
                行为完全等于从前；显式传入时**每次新建一个**——生成器把该开关存在实例上，
                复用一个共享实例就等于「后一个请求悄悄改掉前一个请求的配置」。
                新建时仍复用同一个 LLM 实例（`self.llm`），不会多开客户端。

        Returns:
            `PrdSectionWriter` 实例。
        """
        if review_enabled is None:
            if self._section_writer is None:
                self._section_writer = PrdSectionWriter(
                    llm=self.llm, review_llm=self.review_llm
                )
            return self._section_writer
        return PrdSectionWriter(
            llm=self.llm, review_llm=self.review_llm, review_enabled=review_enabled
        )

    # ------------------------------------------------------------------
    # 模板加载与 Prompt 拼接
    # ------------------------------------------------------------------

    @staticmethod
    def _load_template(name: str) -> str:
        """按名称返回提示词模板。

        模板统一收口在 `services/prompts.py`，这里提供按需取用的入口，便于
        后续扩展（如从文件加载自定义模板）。

        `api_docs` / `prompts` 两类不再走这里：它们的生成提示词由各自的技能包
        提供（`api-docs-generator` 注入团队规范、`prompts-generator` 提供产出要求），
        见 `generate_api_docs_stream` / `generate_prompts_stream`。

        Args:
            name: 模板名，取值 `prd` / `optimize`。

        Returns:
            模板字符串，占位符为 `str.format()` 风格。

        Raises:
            ValueError: 传入了未知的模板名。
        """
        templates: dict[str, str] = {
            "prd": PRD_GENERATION_PROMPT,
            "optimize": OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE,
        }
        if name not in templates:
            raise ValueError(f"未知的文档模板：{name}")
        return templates[name]

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
        artifacts = load_prd_skill_artifacts()
        if artifacts is None:
            # 降级：沿用内置提示词
            user_content = DocumentService._load_template("prd").format(
                requirements=requirements,
                conversation=conversation,
            )
            return SYSTEM_PROMPT, user_content

        system_prompt = build_prd_system_prompt(artifacts["writing_rules"])
        user_content = PRD_SKILL_GENERATION_PROMPT.format(
            requirements=requirements,
            conversation=conversation,
            field_schema=artifacts["field_schema"],
            prd_template=artifacts["prd_template"],
        )
        return system_prompt, user_content

    # ------------------------------------------------------------------
    # 结构化摘要回填（对话 → 摘要）
    # ------------------------------------------------------------------

    async def sync_summary_from_conversation(
        self,
        summary: dict[str, Any],
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """读一遍对话历史，把用户**明确补充或修正**的字段回填进结构化摘要。

        规则（提示词与本地合并双重保证，见 `_merge_summary`）：
        仅当用户明确补充/修正时才更新；不确定的内容保持原值；不凭空新增字段；
        返回**完整**摘要，便于前端做 Diff。

        模型输出非法 JSON 或调用失败时，一律返回原摘要的深拷贝——回填是「锦上添花」
        的一步，不能因为它把用户已填好的需求弄丢。

        Args:
            summary: 当前结构化需求摘要。
            history: 对话历史（`{role, content}` 列表）。

        Returns:
            更新后的完整结构化摘要；无法回填时等价于原摘要。

        Raises:
            Exception: 模型调用失败时向上抛出，由路由层转成 HTTP 错误。
        """
        if not summary:
            return {}

        user_content = SUMMARY_SYNC_PROMPT_TEMPLATE.format(
            summary=json.dumps(summary, ensure_ascii=False, indent=2),
            conversation=format_conversation(history or []),
        )
        messages = self._build_messages(SUMMARY_SYNC_SYSTEM_PROMPT, user_content)
        raw = await self._collect_reply(messages)

        candidate = extract_json_object(raw)
        if candidate is None:
            logger.warning("摘要回填未返回合法 JSON，保持原摘要不变。原始输出：%s", raw[:500])
            return copy.deepcopy(summary)

        return _merge_summary(summary, candidate)

    # ------------------------------------------------------------------
    # 通用流式生成
    # ------------------------------------------------------------------

    async def _collect_reply(self, messages: list[BaseMessage]) -> str:
        """把流式回复收拢成完整文本（用于需要整体结果的非流式场景）。

        供应商抽象只提供 `astream`，需要完整结果时（如摘要回填要解析 JSON）就在
        内部把增量拼起来。同样做截断检查，避免拿到一段被切断的 JSON 后误判。
        """
        return await collect_reply(self.llm, messages)

    # ------------------------------------------------------------------
    # PRD 生成（整篇 / 分章节）
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
            conversation=format_conversation(history or []),
        )
        messages = self._build_messages(system_prompt, user_content)
        async for event in iter_reply_events(
            self.llm,
            messages,
            status_text="正在生成 PRD…",
            extra_data={"doc_type": "prd"},
        ):
            yield event

    async def generate_prd_from_summary_stream(
        self,
        summary: dict[str, Any] | None = None,
        requirements: str = "",
        product_name: str = "",
        history: list[dict[str, Any]] | None = None,
        review_enabled: bool | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """根据**结构化需求摘要**或**自由文本需求**生成 PRD。

        与 `generate_prd_stream` 的区别是入口形态：这里吃的是「与 `field-schema.json`
        对齐的摘要字典」或「用户随手写下的一段话」，需求文本由 `prd_skill` 统一格式化。

        实现上优先走**分章节生成**（`PrdSectionWriter`：逐章撰写 → 审核 → 必要时改写，
        并通过 `stage` 事件把阶段回报给前端）；技能包资源缺失时降级为整篇一次性生成
        （`generate_prd_stream`），保证服务不中断。

        因为入参是「需求本身」而不是「某次会话」，这条链路可被任意入口复用：
        完整流程（传 `summary`）、PRD 快捷入口（传 `requirements` + `product_name`）
        共用同一份契约与同一套写作规范。

        Args:
            summary: 结构化需求摘要（与 field-schema.json 对齐）；给出时优先使用。
            requirements: 自由文本需求；仅在 `summary` 为空时使用。
            product_name: 产品名称，仅用于 PRD 文档头；摘要里已有名称时以摘要为准。
            history: 补充对话记录（`{role, content}` 列表），可为空。
            review_enabled: 本次是否启用**逐章审核 + 改写**。`None`（默认）取配置口径；
                快捷流程显式传 `False`——它的输入只有一段话，逐章复核的边际收益很低，
                而「只撰写、不审不改」能省下约一半调用。

        Yields:
            `StreamEvent`：分章节路径下为 `status` / `chunk` / 若干 `stage` / `done`；
            降级路径下为 `status` / `chunk` / `done`。`done` 的 `data` 带完整 `reply`。

        Raises:
            ValueError: 两种需求输入都为空（路由层已拦一次，这里再兜一层）。
        """
        if not summary and not requirements.strip():
            raise ValueError("summary 与 requirements 至少需要提供一个")

        if prd_sections_available():
            writer = self._get_section_writer(review_enabled)
            async for event in writer.stream(
                summary=summary,
                requirements=requirements,
                product_name=product_name,
                history=history,
            ):
                yield event
            return

        # 降级：技能包缺失或模板章节对不上，退回整篇一次性生成
        logger.warning("PRD 分章节生成不可用，本次降级为整篇生成")
        if summary:
            requirements_text = format_summary_as_requirements(summary)
        else:
            requirements_text = format_freeform_requirements(requirements, product_name)
        async for event in self.generate_prd_stream(
            requirements=requirements_text,
            history=history,
        ):
            yield event

    async def generate_api_docs_stream(
        self,
        prd_content: str,
        review_enabled: bool | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """根据 PRD 正文生成接口文档。

        团队接口规范（统一响应结构、错误码、分页、鉴权等）由 `api-docs-generator` 技能包
        整段提供，生成时全量注入提示词，要求模型按团队约定对齐——不再做「按 PRD 关键词
        挑几条」的 RAG 检索（见 ADR-020）。技能包缺失时退化为「不注入规范」的普通生成，
        并在日志里如实报出。

        Args:
            prd_content: PRD 正文（Markdown）。
            review_enabled: 本次是否跑「整篇审一次 → 有问题改写一次」。`None`（默认）
                视为开启；快捷流程显式传 `False`，一遍过（ADR-034）。

        Yields:
            `StreamEvent`，`done` 事件的 `data` 带完整 `reply`（接口文档正文）。
        """
        artifacts = load_skill_artifacts("api_docs")
        api_specs = artifacts.get("api_specs", "") if artifacts else ""
        if not api_specs:
            logger.warning(
                "api-docs-generator 技能包未加载，接口文档生成将退化为不注入团队规范",
                extra={"event": "api_docs.skill_missing"},
            )
            api_specs = "（团队接口规范未加载，请依据通用 REST 规范与产品 PRD 自行设计）"
        user_content = API_DOCS_GENERATION_PROMPT.format(
            prd_content=prd_content,
            api_specs=api_specs,
            pending_spec=render_spec(),
        )
        messages = self._build_messages(SYSTEM_PROMPT, user_content)
        # 阶段条要**立刻**出现：整篇生成是最长的一步，若等第一次模型调用之后再报阶段，
        # 用户在最长的那段时间里看到的是一片空白（前端因此不知道在干什么）。
        yield StreamEvent(
            type="stage",
            content="正在生成接口文档…",
            data={"stage": "writing", "doc_type": "api_doc"},
        )
        # 标记块可能被切在 chunk 边界：先用抑制器保证实时 chunk 不含哨兵；
        # `done` 时对完整 reply 再 `extract` 一次，得到干净正文与待确认清单。
        suppressor = PendingBlockSuppressor()
        async for event in iter_reply_events(
            self.llm,
            messages,
            status_text="正在生成接口文档…",
            extra_data={"doc_type": "api_doc"},
        ):
            if event.type == "chunk":
                clean = suppressor.feed(event.content)
                if clean:
                    yield StreamEvent(type="chunk", content=clean, data=event.data)
            elif event.type == "done":
                # 流结束，把抑制器残留（不可能是完整块）吐出，保持正文完整；再发 done。
                trailing = suppressor.flush()
                if trailing:
                    yield StreamEvent(type="chunk", content=trailing, data=event.data)
                reply = str(event.data.get("reply", ""))
                clean_reply, items = extract(reply)

                # 整篇生成后审一次；有问题则**改写一次**，不再复审。
                # 审核与改写都是整篇量级的调用，静默期很长，因此两步的原始输出
                # 都通过 `stage_chunk` 实时推给前端（见 `stream_review_and_rewrite`）。
                # 初稿已经以 chunk 流出去过，改写稿用 part{replace} 整段覆盖，
                # 避免「初稿 + 改稿」在同一份预览里叠字。
                #
                # 快捷流程（`review_enabled=False`）**整段跳过**：连 `review` 结论事件
                # 一起不发。只跳调用、照样发一条「通过」的结论，会被读成「审过了、没问题」，
                # 那是假信号（与 PRD 侧同口径）。
                outcome = ReviewOutcome(final_reply=clean_reply, pending_items=items)
                if review_enabled_for_whole_document(review_enabled):
                    async for review_event_or_chunk in stream_review_and_rewrite(
                        self.llm,
                        document=clean_reply,
                        writing_rules=api_specs,
                        # 上游材料骨架化：审核要核对的是「有没有编造 / 漏掉」，
                        # 需要的是事实索引而不是整篇散文
                        requirements=skeletonize(prd_content),
                        doc_type="api_doc",
                        kind="api_docs",
                        label="接口文档",
                        outcome=outcome,
                        review_llm=self.review_llm,
                        section_titles="\n".join(extract_headings(clean_reply)),
                    ):
                        yield review_event_or_chunk
                yield StreamEvent(
                    type="done",
                    content="",
                    data={
                        **(event.data or {}),
                        "reply": outcome.final_reply,
                        "pending_items": outcome.pending_items,
                    },
                )
            else:
                yield event

    async def generate_prompts_stream(
        self,
        prd_content: str,
        api_docs_content: str = "",
        review_enabled: bool | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """根据 PRD 正文（可附带接口文档正文）生成提示词套件。

        产出要求（提示词套件的结构与写法）由 `prompts-generator` 技能包提供，生成时作为
        用户消息模板使用（含 `{prd_content}` / `{api_docs_content}` / `{pending_spec}`
        占位符）；技能包缺失时退回 `prompts.py` 里的内置兜底模板，保证服务不中断。

        Args:
            prd_content: PRD 正文（Markdown）。
            api_docs_content: 接口文档正文，**可选**。给「我有 PRD + 接口文档」入口用：
                两份材料都在手上时，提示词可以同时参考。留空（默认）表示没有这份材料，
                此时模板里那一节填 `NO_API_DOCS_NOTE`——写成「本次未提供」而不是留空，
                免得模型把「空的一节」理解成「漏读了」，转头自己编一份接口契约出来。
            review_enabled: 本次是否跑「整篇审一次 → 有问题改写一次」。`None`（默认）
                视为开启；快捷流程显式传 `False`，一遍过（ADR-034）。

        Yields:
            `StreamEvent`，`done` 事件的 `data` 带完整 `reply`（提示词套件正文）。
        """
        artifacts = load_skill_artifacts("prompts")
        guide = artifacts.get("prompts_guide", "") if artifacts else ""
        if not guide:
            logger.warning(
                "prompts-generator 技能包未加载，使用内置兜底提示词生成提示词套件",
                extra={"event": "prompts.skill_missing"},
            )
            guide = PROMPTS_GENERATION_PROMPT
        # 待确认清单块同样注入：提示词套件也是一份「生成的第一类产物」，与
        # PRD / 接口文档一致地把清单剥离进 `done.data.pending_items`。
        user_content = guide.format(
            prd_content=prd_content,
            api_docs_content=api_docs_content.strip() or NO_API_DOCS_NOTE,
            pending_spec=render_spec(),
        )
        messages = self._build_messages(SYSTEM_PROMPT, user_content)
        # 与接口文档同构：阶段条立即亮起，别让用户在最长的一步里盯着空白
        yield StreamEvent(
            type="stage",
            content="正在生成提示词套件…",
            data={"stage": "writing", "doc_type": "prompts"},
        )
        # 实时 chunk 先过抑制器（块被切在 chunk 边界时只漏安全前缀），`done` 时
        # 对完整 reply 再 `extract` 一次，得到干净正文与待确认清单。
        suppressor = PendingBlockSuppressor()
        async for event in iter_reply_events(
            self.llm,
            messages,
            status_text="正在生成提示词套件…",
            extra_data={"doc_type": "prompts"},
        ):
            if event.type == "chunk":
                clean = suppressor.feed(event.content)
                if clean:
                    yield StreamEvent(type="chunk", content=clean, data=event.data)
            elif event.type == "done":
                trailing = suppressor.flush()
                if trailing:
                    yield StreamEvent(type="chunk", content=trailing, data=event.data)
                reply = str(event.data.get("reply", ""))
                clean_reply, items = extract(reply)

                # 与接口文档同构：整篇生成 → 审一次 → 有问题改写一次（不复审）。
                # 快捷流程（`review_enabled=False`）与接口文档一样整段跳过，不发 `review`。
                # 与接口文档同口径：审核只看骨架，不看整篇
                requirements_text = skeletonize(prd_content)
                if api_docs_content.strip():
                    requirements_text = (
                        f"{requirements_text}\n\n## 接口文档\n\n"
                        f"{skeletonize(api_docs_content)}"
                    )
                outcome = ReviewOutcome(final_reply=clean_reply, pending_items=items)
                if review_enabled_for_whole_document(review_enabled):
                    async for review_event_or_chunk in stream_review_and_rewrite(
                        self.llm,
                        document=clean_reply,
                        writing_rules=guide,
                        requirements=requirements_text,
                        doc_type="prompts",
                        kind="prompts",
                        label="提示词套件",
                        outcome=outcome,
                        review_llm=self.review_llm,
                        section_titles="\n".join(extract_headings(clean_reply)),
                    ):
                        yield review_event_or_chunk
                yield StreamEvent(
                    type="done",
                    content="",
                    data={
                        **(event.data or {}),
                        "reply": outcome.final_reply,
                        "pending_items": outcome.pending_items,
                    },
                )
            else:
                yield event

    async def optimize_document_stream(
        self,
        document: str,
        instruction: str,
        doc_kind: str = "",
    ) -> AsyncIterator[StreamEvent]:
        """按用户指令优化既有文档。

        `doc_kind` 决定注入哪一套产出规范。它是「这次改的是哪一份产物」——没有它就只能
        用降级说明，而回执却会照旧报出一个技能包身份，两边就对不上了（见 `job_service`
        的 `_target_product_kind`）。非 job 通道（老的 `/optimize-document-stream`）不传，
        因此这里给的是空串而不是默认 `prd`：宁可退回「不注入规范」，也不假装注入的是 PRD 那套。

        Args:
            document: 原文档正文（Markdown）。
            instruction: 用户的优化要求。
            doc_kind: 目标产物类型（`prd` / `api_docs` / `prompts`）；为空则不注入规范。

        Yields:
            `StreamEvent`，`done` 事件的 `data` 带完整 `reply`（优化后的文档正文）。
        """
        user_content = self._load_template("optimize").format(
            instruction=instruction,
            document=document,
            skill_context=format_rewrite_reference(doc_kind),
        )
        messages = self._build_messages(SYSTEM_PROMPT, user_content)
        async for event in iter_reply_events(
            self.llm,
            messages,
            status_text="正在优化文档…",
            extra_data={"doc_type": "optimize"},
        ):
            yield event

    async def apply_clarification_stream(
        self,
        document: str,
        history: list[dict[str, Any]] | None = None,
        doc_kind: str = "prd",
    ) -> AsyncIterator[StreamEvent]:
        """按本轮澄清对话改写文档，产出完整正文（含重算后的待确认清单）。

        与 `optimize_document_stream` 的区别在**输入**：优化吃一句用户指令，这里吃一整轮
        澄清对话——用户是在问答里把「待确认」逐条敲定的，结论散在对话记录中，因此必须把
        完整对话交给模型，并明确要求它只动涉及到的章节（见 `CLARIFICATION_REWRITE_PROMPT`）。

        **这里是「改写后重算清单」的落点**：改写输出末尾附一个新的待确认块，实时 `chunk`
        先过抑制器、`done` 时再 `extract` 一次，与三条生成线完全同款——因此「换了正文却
        忘了重算清单」在这条路径上同样没有落脚点。

        Args:
            document: 原文档正文（Markdown）。
            history: 本轮澄清对话记录（`{role, content}` 列表）。
            doc_kind: 目标产物类型，决定注入哪一套产出规范。

        Yields:
            `StreamEvent`；`done` 的 `data` 带完整 `reply` 与 `pending_items`（重算后的清单）。
        """
        user_content = CLARIFICATION_REWRITE_PROMPT.format(
            history=format_conversation(history),
            document=document,
            skill_context=format_rewrite_reference(doc_kind),
            pending_spec=render_spec(),
        )
        async for event in self._rewrite_stream(
            user_content,
            status_text="正在按澄清结果改写文档…",
            doc_type="clarify",
        ):
            yield event

    async def auto_clarify_stream(
        self,
        document: str,
        pending_items: list[dict[str, Any]],
        doc_kind: str = "prd",
    ) -> AsyncIterator[StreamEvent]:
        """让 AI **自己拍板**待确认问题并改写文档（用户不参与）。

        「按澄清对话改写」要求模型**不许编造**（见 `CLARIFICATION_REWRITE_PROMPT` 第四条），
        因为它只能复述用户说过的话；这条链路正相反——用户选择不出面，模型必须**替用户拍板**，
        所以用的是另一套提示词（`AUTO_CLARIFY_PROMPT`）：取最保守的行业默认值，
        拿不准的才留在清单里。

        两条链路共用同一段「剥离标记块 + 重算清单」的收尾（`_rewrite_stream`）：
        改写后的清单可能少几条（拍板掉的），也可能仍有几条（真需要用户提供事实），
        由模型自己决定，前端照单展示。

        Args:
            document: 原文档正文（Markdown）。
            pending_items: 待拍板的清单（后端结构，不是前端拼好的文本）。
            doc_kind: 目标产物类型，决定注入哪一套产出规范。

        Yields:
            `StreamEvent`；`done` 的 `data` 带完整 `reply` 与重算后的 `pending_items`。
        """
        user_content = AUTO_CLARIFY_PROMPT.format(
            document=document,
            pending_items=format_pending_items(pending_items),
            skill_context=format_rewrite_reference(doc_kind),
            pending_spec=render_spec(),
        )
        async for event in self._rewrite_stream(
            user_content,
            status_text="正在逐条拍板待确认问题…",
            doc_type="auto_clarify",
        ):
            yield event

    async def _rewrite_stream(
        self,
        user_content: str,
        *,
        status_text: str,
        doc_type: str,
    ) -> AsyncIterator[StreamEvent]:
        """「改写一份既有文档」的公共收尾：流式输出 + 剥离标记块 + 重算清单。

        澄清改写与 AI 自行澄清只差**提示词**（一个复述用户结论、一个替用户拍板），
        收尾动作完全一样，因此收在这一处：实时 `chunk` 先过抑制器（标记块可能被切在
        分片边界），`done` 时对完整 reply 再 `extract` 一次，得到干净正文与清单。
        两条链路各写一遍的话，「换了正文却忘了重算清单」就有了落脚点。
        """
        messages = self._build_messages(SYSTEM_PROMPT, user_content)
        suppressor = PendingBlockSuppressor()
        async for event in iter_reply_events(
            self.llm,
            messages,
            status_text=status_text,
            extra_data={"doc_type": doc_type},
        ):
            if event.type == "chunk":
                clean = suppressor.feed(event.content)
                if clean:
                    yield StreamEvent(type="chunk", content=clean, data=event.data)
            elif event.type == "done":
                trailing = suppressor.flush()
                if trailing:
                    yield StreamEvent(type="chunk", content=trailing, data=event.data)
                reply = str(event.data.get("reply", ""))
                clean_reply, items = extract(reply)
                yield StreamEvent(
                    type="done",
                    content="",
                    data={
                        **(event.data or {}),
                        "reply": clean_reply,
                        "pending_items": items,
                    },
                )
            else:
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


async def generate_prd_from_summary_stream(
    summary: dict[str, Any] | None = None,
    requirements: str = "",
    product_name: str = "",
    history: list[dict[str, Any]] | None = None,
    review_enabled: bool | None = None,
) -> AsyncIterator[StreamEvent]:
    """根据结构化摘要（或自由文本需求）生成 PRD 的流式入口。"""
    async for event in get_service().generate_prd_from_summary_stream(
        summary=summary,
        requirements=requirements,
        product_name=product_name,
        history=history,
        review_enabled=review_enabled,
    ):
        yield event


async def generate_api_docs_stream(
    prd_content: str,
    review_enabled: bool | None = None,
) -> AsyncIterator[StreamEvent]:
    """生成接口文档的流式入口。"""
    async for event in get_service().generate_api_docs_stream(
        prd_content=prd_content,
        review_enabled=review_enabled,
    ):
        yield event


async def sync_summary_from_conversation(
    summary: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """结构化摘要回填入口：对话历史 → 更新后的完整摘要。"""
    return await get_service().sync_summary_from_conversation(
        summary=summary,
        history=history,
    )


async def generate_prompts_stream(
    prd_content: str,
    api_docs_content: str = "",
    review_enabled: bool | None = None,
) -> AsyncIterator[StreamEvent]:
    """生成提示词套件的流式入口。

    `api_docs_content` 可选：老的 `/generate-prompts-stream` 通道不传，按「只有 PRD」处理。
    """
    async for event in get_service().generate_prompts_stream(
        prd_content=prd_content,
        api_docs_content=api_docs_content,
        review_enabled=review_enabled,
    ):
        yield event


async def optimize_document_stream(
    document: str,
    instruction: str,
    doc_kind: str = "",
) -> AsyncIterator[StreamEvent]:
    """优化文档的流式入口。

    `doc_kind` 决定注入哪一套产出规范；老的 `/optimize-document-stream` 通道不传，
    此时退回「不注入规范」的降级说明。
    """
    async for event in get_service().optimize_document_stream(
        document=document,
        instruction=instruction,
        doc_kind=doc_kind,
    ):
        yield event


async def apply_clarification_stream(
    document: str,
    history: list[dict[str, Any]] | None = None,
    doc_kind: str = "prd",
) -> AsyncIterator[StreamEvent]:
    """按澄清对话改写文档的流式入口（`clarify` 任务走这里）。"""
    async for event in get_service().apply_clarification_stream(
        document=document,
        history=history,
        doc_kind=doc_kind,
    ):
        yield event


async def auto_clarify_stream(
    document: str,
    pending_items: list[dict[str, Any]],
    doc_kind: str = "prd",
) -> AsyncIterator[StreamEvent]:
    """AI 自行拍板待确认问题并改写文档的流式入口（`auto_clarify` 任务走这里）。"""
    async for event in get_service().auto_clarify_stream(
        document=document,
        pending_items=pending_items,
        doc_kind=doc_kind,
    ):
        yield event
