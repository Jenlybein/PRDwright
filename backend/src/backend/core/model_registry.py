"""模型能力注册表：按「供应商 + 型号」登记物理上限。

为什么要有这张表
----------------
预算的「物理顶」由模型本身决定（上下文窗口有多大），是**客观事实**，不该由业务
配置拍脑袋写死。但把某个厂商的窗口直接写进业务代码，又等于把业务绑死在一家
供应商上——换供应商时那段数字就成了暗雷。

因此这里按 **(provider, model)** 分两级登记：

1. 先查**具体型号**（`_KNOWN_MODELS`）；
2. 查不到退到**该 provider 的默认值**（`_PROVIDER_DEFAULTS`）；
3. 连 provider 都没登记，退到 `_FALLBACK`。

表里所有数字都取**保守下界**：宁可把窗口看得小一点、早一点提示用户收束，
也不要等请求打到上游才被厂商拒（那时钱已经花了，而且错误文案来自厂商、不可控）。

**这些数字会过时**——厂商随时会升级窗口、也会新增型号。所以注册表只是**默认值**，
不是事实断言：换模型或窗口变了，改 `LLM_CONTEXT_WINDOW` 环境变量即可覆盖，
不必动代码（见 `core/config.py` 的同名字段）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelLimits:
    """一个型号的物理上限。

    Attributes:
        context_window: 上下文窗口总容量（输入 + 输出合计，单位 token）。
        max_output: 单次回复的输出上限。估算输入预算时把它当作**预留位**
            （给回复留地方），因此可输入的上限是 `context_window - max_output`。
    """

    context_window: int
    max_output: int


# 具体型号：只登记「窗口与 provider 默认值确实不同」的型号，避免为了凑数而瞎填
_KNOWN_MODELS: dict[tuple[str, str], ModelLimits] = {
    # 当前默认型号。登记值取厂商公开文档标注的量级，与实际不符时用
    # LLM_CONTEXT_WINDOW 覆盖——注册表提供默认值，不替厂商做事实担保。
    ("ark", "doubao-seed-2-1-turbo-260628"): ModelLimits(
        context_window=256_000, max_output=32_768
    ),
}

# 供应商级默认值：该供应商下未单独登记的型号一律按此计算。
# 键是**协议名**（`core/providers.py` 的取值域），不是厂商名——一个协议下的
# 厂商共用一组保守下界即可，因为真正精确的值应该由供应商行里的
# `context_window` / `max_output` 填（见 `runtime_config.resolve_model_limits`）。
_PROVIDER_DEFAULTS: dict[str, ModelLimits] = {
    "ark": ModelLimits(context_window=128_000, max_output=4_096),
    "anthropic": ModelLimits(context_window=200_000, max_output=8_192),
    # 旧的厂商名保留一份：有些历史调用点仍按 `openai` 取值，
    # 删掉它会让那些地方静默落到 32k 兜底（预算突然变得很紧）。
    "openai": ModelLimits(context_window=128_000, max_output=16_384),
    "openai_compatible": ModelLimits(context_window=128_000, max_output=16_384),
    "google_genai": ModelLimits(context_window=1_000_000, max_output=65_536),
    "nvidia": ModelLimits(context_window=128_000, max_output=4_096),
    "huggingface": ModelLimits(context_window=64_000, max_output=4_096),
}

# 全局兜底：provider 也没登记时（如将来新增的供应商）用的保守值
_FALLBACK = ModelLimits(context_window=32_000, max_output=4_096)

# 输入上限的下界：窗口减去预留输出后不该退成一个荒唐的小值（配置写错时的护栏）
_MIN_INPUT_CEILING = 1_000


def resolve_limits(provider: str, model: str) -> ModelLimits:
    """查出「供应商 + 型号」的物理上限。

    Args:
        provider: 供应商名（ark / anthropic / openai …）。
        model: 型号名。

    Returns:
        对应的 `ModelLimits`；型号与供应商都未登记时返回 `_FALLBACK`。
    """
    key = (provider.strip().lower(), model.strip())
    known = _KNOWN_MODELS.get(key)
    if known is not None:
        return known
    return _PROVIDER_DEFAULTS.get(key[0], _FALLBACK)


def input_ceiling(provider: str, model: str, *, override: int = 0) -> int:
    """该型号**可用于输入**的 token 上限（物理顶）。

    等于「上下文窗口 − 预留输出」：回复也要占窗口，把整个窗口都当成输入额度会
    让模型写到一半被截断（`finish_reason=length`）。

    Args:
        provider: 供应商名。
        model: 型号名。
        override: 覆盖用的上下文窗口；大于 0 时取代注册表里的窗口值。
            用于「厂商改了窗口 / 用了表里没有的型号」这类情况。

    Returns:
        可用于输入的 token 上限，恒不小于 `_MIN_INPUT_CEILING`。
    """
    limits = resolve_limits(provider, model)
    window = override if override > 0 else limits.context_window
    return max(window - limits.max_output, _MIN_INPUT_CEILING)
