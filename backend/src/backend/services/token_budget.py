"""发送前的 token 预算：估算 → 比上限 → 超了就在调用**之前**拒掉。

为什么光有事前估算还不够、非得有这一层
--------------------------------------
事后计量（`llm_meter`）回答的是「刚才花了多少」，它永远是事后诸葛亮：钱已经付了。
事前预算回答的是「这一次该不该发」——把明显的失控（比如误把整篇 PRD 当作需求
再生成一遍、章节上下文滚雪球）拦在请求发出之前，**这一次调用不产生任何费用**。

有效预算 = min(物理顶, 业务顶)
-----------------------------
- **物理顶**来自 `core/model_registry`：上下文窗口减去预留输出，是模型的客观能力。
- **业务顶**来自 `.env` 按 step 配的 cap（默认 `LLM_BUDGET_DEFAULT_CAP`），
  故意**小于**物理顶——让用户在逼近模型极限之前就先收束，而不是等到上游报错。
- 取两者较小值为有效预算；任何一层收紧都会立刻生效，不需要改代码。

估算为什么不引 tiktoken
----------------------
tiktoken 只覆盖 OpenAI 一家，另外两家要再各配一套映射表，而我们要的只是「量级
对不对」。因此按字符启发式估：中日韩文字按「1 字 ≈ 0.8 token」，其余按
「3.5 字符 ≈ 1 token」，每条消息另计 4 token 的结构开销（角色名与分隔符）。

误差方向刻意偏**高估**：宁可早一点提示用户，也不要低估之后让请求打到上游，
收到一句厂商自己的 `context length exceeded`（那时钱已经花了，文案也不可控）。

超出预算时抛 `BudgetExceededError`，它携带 `INVALID_REQUEST`——错误码体系刻意
维持在 8 个，不为预算新增一个码（见 `core/errors.py`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from langchain_core.messages import BaseMessage

from backend.core.config import Settings, get_settings
from backend.core.errors import AppError, ErrorCode
from backend.core.model_registry import input_ceiling
from backend.services.llm_utils import extract_text

# 中日韩统一表意文字 + 假名 + 谚文：这些文字的 token 密度远高于拉丁字母，
# 混在一起按「字符数 / 4」估算会严重低估中文 prompt 的体积。
_CJK_RE = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힯]")

# 估算系数（宁可高估）
CJK_TOKENS_PER_CHAR = 0.8
OTHER_CHARS_PER_TOKEN = 3.5
# 每条消息的结构开销：角色名、分隔符等不计入正文字符数
MESSAGE_OVERHEAD_TOKENS = 4


# ============================================================================
# 估算
# ============================================================================


def estimate_tokens(text: str) -> int:
    """按字符启发式估算一段文本的 token 数。

    Args:
        text: 任意文本；空串返回 0。

    Returns:
        估算的 token 数（整数，向上取整的保守值）。
    """
    if not text:
        return 0
    cjk = sum(1 for _ in _CJK_RE.finditer(text))
    other = len(text) - cjk
    return int(cjk * CJK_TOKENS_PER_CHAR + other / OTHER_CHARS_PER_TOKEN)


def estimate_messages_tokens(messages: Sequence[BaseMessage]) -> int:
    """估算一次调用的输入 token 数（含每条消息的结构开销）。

    分块的 `content` 可能是结构化列表（Anthropic 风格），`extract_text` 统一
    摊平成字符串后再估，与运行时取文本走同一条归一化路径。

    Args:
        messages: 已组装好的 LangChain 消息列表。

    Returns:
        估算的 token 数；空消息列表返回 0。
    """
    total = 0
    for message in messages:
        total += estimate_tokens(extract_text(getattr(message, "content", "")))
        total += MESSAGE_OVERHEAD_TOKENS
    return total


# ============================================================================
# 上限
# ============================================================================


def step_cap(step: str, settings: Settings | None = None) -> int:
    """取某个 step 的业务顶。

    查找顺序：**完整 step**（`prd:writing`）→ **任务类型**（`prd`，即 step 里
    冒号前的部分）→ `llm_budget_default_cap`。这样既能给某一步单独收紧，也能
    用一条 `prd=60000` 管住整条 PRD 流水线，不必逐阶段列一遍。

    Args:
        step: 步骤名，形如 `prd:writing`；未知或空串时用默认值。
        settings: 配置；省略时取进程内单例。

    Returns:
        该 step 的业务顶（token）。
    """
    settings = settings or get_settings()
    caps = settings.budget_step_caps
    if step in caps:
        return caps[step]
    kind = step.split(":", 1)[0]
    if kind in caps:
        return caps[kind]
    return settings.llm_budget_default_cap


# ============================================================================
# 判定
# ============================================================================


@dataclass(frozen=True)
class BudgetDecision:
    """一次调用的事前预算判定。

    Attributes:
        step: 步骤名（决定用哪条业务顶）。
        estimated_tokens: 估算的输入 token 数。
        physical_ceiling: 物理顶（模型窗口减去预留输出）。
        business_cap: 业务顶（配置里给的上限）。
        budget: 有效预算 = min(物理顶, 业务顶)。
        warn_at: 告警阈值 = 有效预算 × 告警比例。
    """

    step: str
    estimated_tokens: int
    physical_ceiling: int
    business_cap: int
    budget: int
    warn_at: int

    @property
    def exceeded(self) -> bool:
        """是否超顶：超了就不该发这次调用。"""
        return self.estimated_tokens > self.budget

    @property
    def warned(self) -> bool:
        """是否逼近上限（未超顶但已达告警阈值）。"""
        return not self.exceeded and self.estimated_tokens >= self.warn_at

    @property
    def ratio(self) -> float:
        """已用占预算的比例（0~1+）；预算为 0 时返回 0，避免除零。"""
        return self.estimated_tokens / self.budget if self.budget > 0 else 0.0

    def rejection_message(self) -> str:
        """超顶时的中文说明：讲清「超了多少、上限是谁定的」，并给出下一步。"""
        return (
            f"本次输入约 {self.estimated_tokens:,} tokens，超过预算上限 {self.budget:,}"
            f"（物理顶 {self.physical_ceiling:,} / 业务顶 {self.business_cap:,}），"
            "已停止调用以免浪费。请精简需求描述、或缩短已生成内容后重试。"
        )

    def warning_message(self) -> str:
        """逼近上限时的中文提示：只作展示，前端不做分支判断。"""
        label = self.step or "本次调用"
        return (
            f"{label} 的输入约 {self.estimated_tokens:,} tokens，"
            f"已达预算上限 {self.budget:,} 的 {int(self.ratio * 100)}%"
        )


class BudgetExceededError(AppError):
    """输入超出预算，本次调用**未发出**。

    刻意复用 `INVALID_REQUEST`：错误码体系维持在 8 个，预算超顶对前端来说就是
    「这次请求不合法、重试也不会变好」，与「上游挂了可以重试」是两类处理。
    """

    def __init__(self, decision: BudgetDecision) -> None:
        super().__init__(ErrorCode.INVALID_REQUEST, decision.rejection_message())
        self.decision = decision


# ============================================================================
# 入口
# ============================================================================


def evaluate(
    messages: Sequence[BaseMessage],
    *,
    provider: str = "",
    model: str = "",
    step: str = "",
) -> BudgetDecision:
    """估算并比对上限，返回判定结果（**不做任何拦截**）。

    Args:
        messages: 待发送的消息。
        provider: 供应商；留空时取配置的默认供应商。
        model: 型号；留空时取配置的默认型号。
        step: 步骤名；留空时按默认业务顶计算。

    Returns:
        `BudgetDecision`。
    """
    settings = get_settings()
    provider = provider or settings.default_llm_provider
    model = model or settings.default_llm_model
    physical = input_ceiling(provider, model, override=settings.llm_context_window)
    business = step_cap(step, settings)
    budget = max(min(physical, business), 1)
    return BudgetDecision(
        step=step,
        estimated_tokens=estimate_messages_tokens(messages),
        physical_ceiling=physical,
        business_cap=business,
        budget=budget,
        warn_at=int(budget * settings.llm_budget_warn_ratio),
    )


def enforce(
    messages: Sequence[BaseMessage],
    *,
    provider: str = "",
    model: str = "",
    step: str = "",
) -> BudgetDecision:
    """估算、比对，并在超顶时抛出（调用方据此不发出请求）。

    Args:
        同 `evaluate`。

    Returns:
        未超顶时的 `BudgetDecision`（调用方可用它决定是否记一条告警）。

    Raises:
        BudgetExceededError: 估算值超过有效预算。此时**尚未调用模型**。
    """
    decision = evaluate(messages, provider=provider, model=model, step=step)
    if decision.exceeded:
        raise BudgetExceededError(decision)
    return decision
