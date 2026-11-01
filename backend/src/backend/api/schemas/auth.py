"""认证（登录 / 注册 / 会话）的请求与响应。

从 `backend.api.schemas` 拆出（#7）；对外引用由包的 `__init__` 再导出。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ============================================================================
# 认证（登录 / 注册 / 会话）
# ============================================================================


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str = Field(min_length=1, description="用户名")
    password: str = Field(min_length=1, description="密码")


class RegisterRequest(BaseModel):
    """注册请求。

    注册**需要邀请码**——当前是开放注册，但用邀请码挡住随手批量注册的滥用。
    """

    username: str = Field(min_length=3, max_length=32, description="用户名，3-32 个字符")
    password: str = Field(min_length=8, description="密码，至少 8 位")
    invite_code: str = Field(min_length=1, description="邀请码")


class AuthUserResponse(BaseModel):
    """用户基本信息。刻意**不含**任何敏感字段（如密码哈希、salt）。"""

    id: int = Field(description="用户 ID")
    username: str = Field(description="用户名")


class LogoutResponse(BaseModel):
    """登出响应。"""

    ok: bool = Field(default=True, description="是否成功登出")
