"""请求级上下文：给一次 HTTP 请求一个可追踪的 `request_id`。

**为什么单独一个模块**：`request_id` 的产生端在 `api/`（中间件），消费端在 `services/`
（生成任务把 request_id 写进 `run_summary`）与后续的结构化日志。放在 `core/`
既不违反分层方向（`api → services → core`），也不必让服务层认识任何一个具体的中间件。

**为什么用 `ContextVar`**：中间件与路由之间没有参数通道——纯 ASGI 中间件只在 `scope`
里能留下东西，而需要在服务层（深层调用）读到的值不可能靠一路传参。`ContextVar` 是
asyncio 里传递这类「隐式上下文」的标准做法，且**任务边界会自然复制上下文**：
`job_service.dispatch` 在请求上下文内 `asyncio.create_task`，后台任务于是自动带上
同一个 `request_id`，无需任何额外接线。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

# 当前请求的 id；无请求上下文（后台任务直接构造、脚本、测试）时为空串。
_request_id: ContextVar[str] = ContextVar("request_id", default="")


def set_request_id(value: str) -> Token[str]:
    """写入当前上下文的 request_id，返回可用于还原的 token。

    由请求中间件调用；`finally` 里必须 `reset_request_id(token)`，
    否则同一个事件循环上被复用的上下文会串号。
    """
    return _request_id.set(value)


def reset_request_id(token: Token[str]) -> None:
    """还原到 `set_request_id` 之前的取值。"""
    _request_id.reset(token)


def current_request_id() -> str:
    """取当前上下文的 request_id；没有时返回空串（**不抛异常**）。

    判断「要不要输出 request_id」由调用方决定：生成任务在无请求来源时
    （如测试直接调服务）照常工作，只是摘要里这一项为空。
    """
    return _request_id.get()
