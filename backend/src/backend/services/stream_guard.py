"""流式输出截断检测。

统一判断各供应商的流式回复是否因达到 `max_tokens` 上限而被截断。

不同供应商对「正常结束」与「被截断」的标记不同：

- 火山方舟 / OpenAI（兼容协议）：`choices[0].finish_reason`，截断值为 `length`。
- Anthropic：顶层 `stop_reason`，截断值为 `max_tokens`。

LangChain 会把这两者统一收进 `AIMessageChunk.response_metadata` 里
（方舟封装 `ark_llm` 也会主动把 `finish_reason` 写入该字段），所以业务层
只需关注 `response_metadata`，无需感知底层供应商。
"""

from __future__ import annotations

from langchain_core.messages import AIMessageChunk

# 各供应商「被截断」的标记值
_TRUNCATED_MARKERS = {"length", "max_tokens"}


class OutputTruncatedError(RuntimeError):
    """模型输出因达到 max_tokens 上限而被截断。"""

    def __init__(self, reason: str = "length") -> None:
        super().__init__(
            f"模型输出达到 token 上限被截断（finish_reason/stop_reason = {reason}）。"
            "请调大 max_tokens，或缩短输入 / 拆分文档后再生成。"
        )
        self.reason = reason


def extract_finish_reason(chunk: AIMessageChunk) -> str:
    """从单个流式分块里提取结束原因，兼容各供应商的字段差异。

    优先读 `response_metadata`，再回退到 `additional_kwargs`；二者都没有时
    返回空串。
    """
    meta = getattr(chunk, "response_metadata", None) or {}
    extra = getattr(chunk, "additional_kwargs", None) or {}

    # Anthropic：stop_reason 在 additional_kwargs 或 response_metadata 里
    stop_reason = meta.get("stop_reason") or extra.get("stop_reason")
    if stop_reason:
        return str(stop_reason)

    # OpenAI / 方舟：finish_reason 在 response_metadata 里
    finish_reason = meta.get("finish_reason") or extra.get("finish_reason")
    return str(finish_reason) if finish_reason else ""


def is_truncated(reason: str) -> bool:
    """判断结束原因是否为「被截断」（length / max_tokens）。"""
    return reason.strip().lower() in _TRUNCATED_MARKERS


def ensure_not_truncated(chunk: AIMessageChunk) -> None:
    """检查单个分块是否携带截断标记，是则抛出 `OutputTruncatedError`。

    供业务层在遍历完流后，对最后一个（或每一个）分块调用，统一拦截截断。
    """
    reason = extract_finish_reason(chunk)
    if is_truncated(reason):
        raise OutputTruncatedError(reason)


# 复述判据只看正文开头这么多字符就够：真出问题时，模型是整段照搬对话，开头一定重合。
_ECHO_PROBE_CHARS = 80

# 少于这么多字符的开头不作为证据（「1.」「好的」这类短串在任何文本里都可能出现），
# 判据必须能证伪，不能靠一个大词表去猜。
_ECHO_MIN_CHARS = 24


def echoes_reference(text: str, reference: str) -> bool:
    """判断模型输出是不是**照着喂进去的参考材料原样复述**。

    线上真出现过一次：PRD 的撰写调用把上一轮澄清回复整段当正文吐了出来（开头是
    「已经拿到你这份结构化需求……」），随后审核还在按 PRD 的标准给这段对话挑毛病、
    改写再改一遍，最后当成一版 PRD 落库。提示词里写「不要复述对话」只是软约束。

    判据刻意只认**开头与参考材料逐字重合**，不要求「必须是 `# ` 开头的文档」：
    - 后者会把任何短正文都判成假的（夹具与真正的短文档都可能只有一层标题），
      误杀代价远高于漏判；
    - 而「照抄参考材料」是一个能证伪的强信号：正文开头几十个字原样出现在
      对话记录里，说明模型在续写对话，不是在写文档。

    Args:
        text: 模型输出的完整正文。
        reference: 本次喂给模型的参考材料（PRD 是补充对话记录；没有就传空串）。

    Returns:
        `True` 表示疑似复述（应当作废重试）。
    """
    head = " ".join((text or "").split())[:_ECHO_PROBE_CHARS]
    if len(head) < _ECHO_MIN_CHARS:
        return False
    haystack = " ".join((reference or "").split())
    if not haystack:
        return False
    return head in haystack
