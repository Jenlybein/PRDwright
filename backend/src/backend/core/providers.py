"""供应商协议：把「接哪一家」抽象成「用哪套协议」。

为什么不按厂商建模
------------------
LangChain 的 chat 集成有二十来家独立包，但按**协议**归类只有六种。按厂商建枚举的话，
接一家新厂商就要加一个枚举值 + 一个分支 + 一个依赖；按协议建，接一家 OpenAI 兼容的
新厂商只是**填一行数据**（`base_url` + 密钥变量名 + 模型名），代码零改动。

已经验证的事实（决定了这个建模）：`langchain-deepseek`、`langchain-groq`、
`langchain-together` 等包都是 `BaseChatOpenAI` 的薄封装——它们做的事只是把
`base_url` 写死。既然本项目已经把 `base_url` 做成可配字段，就没有理由再为
每家装一个包。

六个协议与各自的适配器
----------------------
| protocol            | 用谁                                   | 备注 |
|---------------------|----------------------------------------|------|
| `openai_compatible` | `langchain_openai.ChatOpenAI`          | 覆盖绝大多数厂商，含国内主流 |
| `anthropic`         | `langchain_anthropic.ChatAnthropic`    | 原生 SDK，无 `response_format` |
| `ark`               | 自研 `VolcanoArkChatModel`             | 流式用量帧特殊，必须自研 |
| `google_genai`      | `langchain_google_genai.ChatGoogleGenerativeAI` | 参数名是 `max_output_tokens` |
| `nvidia`            | `langchain_nvidia_ai_endpoints.ChatNVIDIA` | 无 `timeout` 参数 |
| `huggingface`       | `ChatHuggingFace` 包 `HuggingFaceEndpoint` | **两段式**构造 |

密钥怎么存（ADR-053）
--------------------
`ProviderSpec.value_token` 是**加密后的密钥密文**（主密钥在
`backend/secrets/master.key`，见 `core/crypto.py`）。数据库里只有密文，
env 里不放密钥；取值在构造 LLM 的前一刻解密。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

# 六个协议。新增协议 = 加一个值 + 在 `llm_factory` 加一个分支，
# 这是**唯一**需要改代码的扩展方式；加厂商不需要。
Protocol = Literal[
    "openai_compatible",
    "anthropic",
    "ark",
    "google_genai",
    "nvidia",
    "huggingface",
]

PROTOCOLS: tuple[str, ...] = (
    "openai_compatible",
    "anthropic",
    "ark",
    "google_genai",
    "nvidia",
    "huggingface",
)

# 展示名：后台下拉框直接用，不把蛇形标识符露给管理员
PROTOCOL_LABELS: dict[str, str] = {
    "openai_compatible": "OpenAI 兼容（DeepSeek / Kimi / GLM / 通义 / vLLM 等）",
    "anthropic": "Anthropic 原生（Claude）",
    "ark": "火山方舟",
    "google_genai": "Google Gemini",
    "nvidia": "NVIDIA NIM",
    "huggingface": "Hugging Face",
}

# ---------------------------------------------------------------------------
# 配置条目的身份
# ---------------------------------------------------------------------------
#
# `model_providers.id` 现在的语义是「**一条配置**的身份」，不是「一家供应商」的身份。
# 同一家（同一协议）可以有多条配置，所以 id 必须由后端生成——让管理员填，
# 两个人都想加一条方舟配置时必然撞名，而撞名之后唯一能做的决定是「谁让」。
#
# 前缀不是装饰：日志、审计、回执里一串裸十六进制谁也认不出，`cfg_` 一眼就能
# 看出这是「库里的配置条目」。
CONFIG_ID_PREFIX = "cfg_"


def new_config_id() -> str:
    """生成一条配置的 id：`cfg_` + 10 位十六进制（40 bit）。

    40 bit 在「几百条配置」的规模下碰撞概率可以忽略，但**不因此省掉兜底**：
    仓储层的 `INSERT` 撞主键会抛 `IntegrityError`，服务层捕获后重试。
    「先查再写」看着更严谨，其实有竞态——两个并发请求都能查到「不存在」。
    """
    return CONFIG_ID_PREFIX + uuid4().hex[:10]


@dataclass(frozen=True)
class ProviderSpec:
    """一条模型配置的完整解析结果（供构造 LLM 用）。

    刻意做成不可变：它由「DB 行 + 密钥是否存在的探测结果」拼出来，
    一旦构造就该代表**那一刻**的事实。可变的话，一个任务中途改配置就会
    让调用方手里的 spec 与真实配置悄悄分叉。

    字段分工（这三者混用是最容易犯的错）：

    - `id` 是**配置条目**的身份：`cfg_xxx`；env 合成出来的走 `env:xxx`。
      它只用于「指向哪一条配置」——缓存指纹、激活指针、回执里的
      `provider_config_id`。**绝不能拿去查模型限值**。
    - `protocol` 是**协议**（六选一），决定用哪个构造器、查哪一组限值默认值。
      日志、健康检查、预算都认它。
    - `name` 是给人看的配置名（「方舟-主力」），可以重名、可以改。
    """

    id: str
    name: str
    protocol: str
    base_url: str
    value_token: str  # 加密后的密钥密文（主密钥解密）；空=未配置
    model: str
    max_tokens: int
    context_window: int = 0
    max_output: int = 0
    thinking: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key_hint(self) -> str:
        """缺密钥时给人看的提示。

        不要只说「未配置密钥」——要告诉人去哪儿配。密钥现在只在管理后台录入，
        所以提示里直接点名那个页面。
        """
        return (
            f"供应商「{self.name or self.id}」尚未配置密钥，"
            f"请在管理后台「模型配置」页填入后重试"
        )

    def with_model(self, model: str) -> ProviderSpec:
        """换一个模型名，返回新 spec（`get_llm(model=...)` 的覆盖路径）。"""
        return ProviderSpec(
            id=self.id,
            name=self.name,
            protocol=self.protocol,
            base_url=self.base_url,
            value_token=self.value_token,
            model=model or self.model,
            max_tokens=self.max_tokens,
            context_window=self.context_window,
            max_output=self.max_output,
            thinking=self.thinking,
            extra=self.extra,
        )

    def with_max_tokens(self, max_tokens: int) -> ProviderSpec:
        """换输出上限，返回新 spec。

        审核链路要用更小的窗口（`review_llm`），而方舟的 `max_tokens` 只能靠
        **实例**生效（`astream(max_tokens=...)` 不生效），所以必须能造出一个
        只改了上限的 spec。
        """
        return ProviderSpec(
            id=self.id,
            name=self.name,
            protocol=self.protocol,
            base_url=self.base_url,
            value_token=self.value_token,
            model=self.model,
            max_tokens=int(max_tokens),
            context_window=self.context_window,
            max_output=self.max_output,
            thinking=self.thinking,
            extra=self.extra,
        )


def spec_from_row(row: dict[str, Any]) -> ProviderSpec:
    """把 `model_providers` 的一行转成 `ProviderSpec`。

    `extra_json` 的解码交给 `repositories.runtime_config_repo.decode_extra`
    （调用方传入已解码的 dict 更省事，这里也兼容裸字符串）。
    """
    extra = row.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    return ProviderSpec(
        id=str(row.get("id") or ""),
        name=str(row.get("name") or ""),
        protocol=str(row.get("protocol") or "openai_compatible"),
        base_url=str(row.get("base_url") or ""),
        value_token=str(row.get("value_token") or ""),
        model=str(row.get("default_model") or ""),
        max_tokens=int(row.get("max_tokens") or 4096),
        context_window=int(row.get("context_window") or 0),
        max_output=int(row.get("max_output") or 0),
        thinking=bool(row.get("thinking_enabled")),
        extra=extra,
    )


# 新建配置时的模板：选一家就把地址、密钥变量名、建议模型填好。
#
# **模板不入库**：它只是表单的默认值，选完随表单一并提交，库里只留最终结果。
# （v16 曾经把「每个协议一行」直接播进 `model_providers`，后果是后台一进去就
# 摆着六张空卡片，且「同一家只能有一条」成了表结构的隐含约束——v19 已废弃该做法。）
#
# `key` 只是模板标识（前端下拉用的 value），**不是**配置 id：配置 id 由
# `new_config_id()` 生成。这两者一旦混用，就会退回「一家只能一条」的老路。
#
# 地址会变，以官方文档为准；这里给的是「能跑通」的起点，填完仍可改。
PRESETS: list[dict[str, str]] = [
    # —— 常见厂商与本地部署 ——
    {
        "key": "ark",
        "name": "火山方舟",
        "protocol": "ark",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "",
    },
    {
        "key": "deepseek",
        "name": "DeepSeek",
        "protocol": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
    },
    {
        "key": "moonshot",
        "name": "Moonshot / Kimi",
        "protocol": "openai_compatible",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "",
    },
    {
        "key": "zhipu",
        "name": "智谱 GLM",
        "protocol": "openai_compatible",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "",
    },
    {
        "key": "dashscope",
        "name": "阿里通义（百炼）",
        "protocol": "openai_compatible",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "",
    },
    {
        "key": "openrouter",
        "name": "OpenRouter",
        "protocol": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "",
    },
    {
        "key": "vllm",
        "name": "本地 vLLM / Ollama",
        "protocol": "openai_compatible",
        "base_url": "http://127.0.0.1:8000/v1",
        "default_model": "",
    },
    {
        # 与上一条**并列存在**，不是重复：本地推理服务不要密钥，而企业内网网关
        # 通常是 OpenAI 兼容 + Bearer token。管理界面不再给「自定义」入口（变量名
        # 也不让填），所以需要密钥的自建接入点必须有这么一条预设，否则是死路。
        "key": "selfhosted",
        "name": "自建网关（OpenAI 兼容，需密钥）",
        "protocol": "openai_compatible",
        "base_url": "",
        "default_model": "",
    },
    # —— 其余五个协议各给一个模板，否则「新建一条方舟/Claude」只能手填全部字段 ——
    {
        "key": "anthropic",
        "name": "Anthropic Claude",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com",
        "default_model": "",
    },
    {
        "key": "gemini",
        "name": "Google Gemini",
        "protocol": "google_genai",
        "base_url": "",
        "default_model": "",
    },
    {
        "key": "nvidia",
        "name": "NVIDIA NIM",
        "protocol": "nvidia",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "default_model": "",
    },
    {
        "key": "huggingface",
        "name": "Hugging Face",
        "protocol": "huggingface",
        "base_url": "",
        "default_model": "",
    },
]

