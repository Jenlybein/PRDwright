"""跨层错误契约：机器可读的错误码与统一异常。

**为什么放在 `core/` 而不是 `api/`**：抛出异常的主要是 `services/`。若契约定义在
`api/` 里，服务层就要反向依赖接口层，那是分层倒挂，而且会让 `services` 的单元
测试必须先把 FastAPI 拉起来。放 `core/` 则上下两层都只依赖这份契约本身。

**契约形态**：HTTP 错误响应体在既有 `detail`（中文原文，给人和日志看）之外**并列**
新增 `code`（机器可读，给前端分支判断用）。`detail` 一字不改是向后兼容的前提——
现有测试与前端兜底文案都按原文断言，改一个字就会静默失效。

SSE 流内的 `error` 事件同样带 `code`（见 `api/schemas.py` 的 `StreamEvent.code`）。
这一点是本次改造的核心：像「会话不存在」这类错误发生在 HTTP 200 的流里，
前端拿不到状态码，只能靠 `code` 判断。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

# ============================================================================
# 错误码
# ============================================================================


class ErrorCode(str, Enum):
    """对外承诺的错误码。

    取值一旦发布就是**契约**，前端 `frontend/src/services/errors.ts` 的
    `BACKEND_ERROR_CODES` 与之严格一致；改名或增删都要两侧同步，否则前端的类型
    守卫会静默失配。

    成员数量刻意保持在 9 个：只覆盖前端真正需要**区分对待**的分支，
    其余一律归入 `INTERNAL_ERROR`，避免错误码膨胀成第二套异常体系。
    """

    # 会话在服务内存里已不存在（服务重启 / 多实例）。只出现在 SSE error 事件里，
    # 因为此时 HTTP 状态码仍是 200。前端据此清掉失效会话 ID 后无状态重试。
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    # 未登录或登录态过期。HTTP 401。
    UNAUTHORIZED = "UNAUTHORIZED"
    # 已登录但权限不足（典型：非管理员访问 `/api/admin/**`）。HTTP 403。
    # 与 `UNAUTHORIZED` 分开的理由是前端对两者的处理**完全不同**：401 要跳登录，
    # 403 是「登录了也不行」，只能停在无权限页。合成一个码的话前端只能给同一句
    # 文案，用户会反复被踢去登录却永远登不进去。
    FORBIDDEN = "FORBIDDEN"
    # 注册 / 登录失败的全部分支：用户名或密码不合规、缺邀请码、用户名已注册、
    # 邀请码无效、注册失败、密码错误、账号停用。HTTP 400（注册）/ 401（登录）。
    AUTH_FAILED = "AUTH_FAILED"
    # 方案不存在，或存在但不属于当前用户（两者对外不可区分）。HTTP 404。
    SOLUTION_NOT_FOUND = "SOLUTION_NOT_FOUND"
    # 请求语义不合法，但字段格式没问题（如 summary 与 requirements 皆空）。HTTP 400。
    INVALID_REQUEST = "INVALID_REQUEST"
    # Pydantic 字段校验失败。HTTP 422，`detail` 保持 FastAPI 的列表结构。
    VALIDATION_ERROR = "VALIDATION_ERROR"
    # 上游大模型调用失败（摘要回填失败、流式生成失败）。HTTP 502 / SSE error。
    LLM_UPSTREAM_ERROR = "LLM_UPSTREAM_ERROR"
    # 其余服务端故障（问卷配置缺失或非法 JSON、检索失败、SSE 兜底）。
    # HTTP 500 / SSE error，同时是前端遇到未知 code 时的降级值。
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ============================================================================
# 错误码 → HTTP 状态码
# ============================================================================

# 缺省状态码。只收录「以 HTTP 响应体为载体」的错误码：
# `SESSION_NOT_FOUND` 不在其中，它只出现在 HTTP 200 的 SSE 流内，没有对应的状态码。
DEFAULT_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.AUTH_FAILED: 401,
    ErrorCode.SOLUTION_NOT_FOUND: 404,
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.LLM_UPSTREAM_ERROR: 502,
    ErrorCode.INTERNAL_ERROR: 500,
}

# `DEFAULT_STATUS` 未收录的错误码（当前只有 `SESSION_NOT_FOUND`）被当作 HTTP 异常
# 抛出时的兜底状态码。正常链路里不会发生——它只用于「有人误把流内错误码拿去抛
# HTTP 异常」时，让请求以明确的 500 失败，而不是让服务端崩在查表上。
_FALLBACK_STATUS = 500


# ============================================================================
# 统一异常
# ============================================================================


class AppError(Exception):
    """携带错误码的业务异常，由 `main.py` 的异常处理器统一渲染成 JSON。

    为什么不用 `HTTPException(detail={"detail": ..., "code": ...})`：FastAPI 会把
    `detail` 原样塞进响应体的 `detail` 字段，最终渲染成
    `{"detail": {"detail": ..., "code": ...}}` 这种嵌套结构，前端要多剥一层，
    而且 `detail` 的语义被稀释了。走自定义处理器才能保证 `detail` 与 `code` 并列。

    Attributes:
        code: 机器可读的错误码。
        message: 可直接展示给用户的中文说明，渲染后即响应体的 `detail`。
        status_code: HTTP 状态码；省略时按 `DEFAULT_STATUS` 取默认值。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = DEFAULT_STATUS.get(code, _FALLBACK_STATUS) if status_code is None else status_code

    def __str__(self) -> str:
        """带上错误码，方便日志里一眼看出分类。"""
        return f"[{self.code.value}] {self.message}"


# ============================================================================
# 响应体构造
# ============================================================================


def error_payload(detail: Any, code: ErrorCode) -> dict[str, Any]:
    """构造统一的错误响应体：`detail` 与 `code` 并列。

    Args:
        detail: 响应体的 `detail`。字符串或 FastAPI 校验错误列表，取值与措辞
            由调用方决定——本函数不做任何文案改写，以保证向后兼容。
        code: 机器可读的错误码。

    Returns:
        形如 `{"detail": ..., "code": "..."}` 的字典。
    """
    return {"detail": detail, "code": code.value}
