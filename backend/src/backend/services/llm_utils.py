"""模型输出的通用处理工具。

流式与一次性两种调用方式下，业务层都要做同一件事：把供应商返回的分块内容
归一化成纯文本，或从模型输出里尽力抠出一个 JSON 对象。这些处理与具体业务无关，
集中在这里可以避免各 service 各写一份（尤其是 JSON 提取，四家供应商的
代码块围栏习惯各不相同）。

不依赖任何业务模块，可被任意 service 直接导入。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 模型输出里常见的 Markdown 代码块围栏（```json / ``` 两种写法）
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_text(content: Any) -> str:
    """从流式分块的内容里取出纯文本。

    供应商抽象层的 `astream` 返回的 `content` 有两种形态：纯字符串，或
    `[{"type": "text", "text": "..."}]` 这样的结构化列表（Anthropic 风格）。
    这里把两种都归一成字符串，其它类型返回空串（宁可丢一个分块，也不抛异常
    打断整条流）。

    Args:
        content: 分块携带的 `content` 字段。

    Returns:
        归一化后的文本；无法识别时为 `""`。
    """
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


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里尽力取出一个 JSON 对象。

    依次尝试：代码块围栏内容 → 首个 `{` 到最后一个 `}` 的片段。都失败时返回
    `None`，由调用方决定兜底策略（如「保持原值」或「视为通过」）。

    Args:
        text: 模型原始输出。

    Returns:
        解析出的字典；无法解析时返回 `None`。
    """
    candidates: list[str] = []
    fenced = _JSON_FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)

    for candidate in candidates:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
