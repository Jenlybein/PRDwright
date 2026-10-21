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

密钥永不入库
------------
`ProviderSpec.api_key_env_var` 只是「去哪个环境变量取」的指针，取自环境或 `.env`
（见 `core/secrets.py`）。数据库里没有、也不该有任何明文密钥（ADR-042）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

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

# 内置三家的密钥变量名。**这是唯一的映射点**：`Settings.api_key_env_var()` 与
# 健康检查的报错文案都引用它，新增内置供应商只改这里一处。
API_KEY_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "ark": "ARK_API_KEY",
    "openai": "OPENAI_API_KEY",
}


@dataclass(frozen=True)
class ProviderSpec:
    """一家供应商的完整解析结果（供构造 LLM 用）。

    刻意做成不可变：它由「DB 行 + 密钥是否存在的探测结果」拼出来，
    一旦构造就该代表**那一刻**的事实。可变的话，一个任务中途改配置就会
    让调用方手里的 spec 与真实配置悄悄分叉。
    """

    id: str
    name: str
    protocol: str
    base_url: str
    api_key_env_var: str
    model: str
    max_tokens: int
    context_window: int = 0
    max_output: int = 0
    thinking: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key_env_hint(self) -> str:
        """缺密钥时给人看的提示。

        一定要带**变量名**：只说「未配置密钥」会让人去翻 `.env` 找半天，
        而答案就在这句话里。
        """
        if not self.api_key_env_var:
            return f"供应商「{self.name or self.id}」未指定密钥环境变量名"
        return (
            f"供应商「{self.name or self.id}」尚未配置 {self.api_key_env_var}，"
            f"请在 .env 中设置后重启（或确认已导出到进程环境）"
        )

    def with_model(self, model: str) -> ProviderSpec:
        """换一个模型名，返回新 spec（`get_llm(model=...)` 的覆盖路径）。"""
        return ProviderSpec(
            id=self.id,
            name=self.name,
            protocol=self.protocol,
            base_url=self.base_url,
            api_key_env_var=self.api_key_env_var,
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
            api_key_env_var=self.api_key_env_var,
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
        api_key_env_var=str(row.get("api_key_env_var") or ""),
        model=str(row.get("default_model") or ""),
        max_tokens=int(row.get("max_tokens") or 4096),
        context_window=int(row.get("context_window") or 0),
        max_output=int(row.get("max_output") or 0),
        thinking=bool(row.get("thinking_enabled")),
        extra=extra,
    )


# 新增供应商时的预设模板：选一家常见厂商就把地址与变量名填好，
# 管理员只需要填密钥值。**地址会变**——界面上一并提示「以官方文档为准」，
# 不要把它当事实断言（这也是为什么它只是「预设」，填完仍可改）。
PRESETS: list[dict[str, str]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "protocol": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env_var": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
    },
    {
        "id": "moonshot",
        "name": "Moonshot / Kimi",
        "protocol": "openai_compatible",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env_var": "MOONSHOT_API_KEY",
        "default_model": "",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "protocol": "openai_compatible",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env_var": "ZHIPU_API_KEY",
        "default_model": "",
    },
    {
        "id": "dashscope",
        "name": "阿里通义（百炼）",
        "protocol": "openai_compatible",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env_var": "DASHSCOPE_API_KEY",
        "default_model": "",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "protocol": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env_var": "OPENROUTER_API_KEY",
        "default_model": "",
    },
    {
        "id": "vllm",
        "name": "本地 vLLM / Ollama",
        "protocol": "openai_compatible",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key_env_var": "",
        "default_model": "",
    },
]
