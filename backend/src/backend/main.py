"""FastAPI 应用入口。

本地启动（在 backend/ 目录下执行）：

    uv run uvicorn backend.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.auth import router as auth_router
from backend.api.config import router as config_router
from backend.api.conversation import router as conversation_router
from backend.api.deps import AuthMiddleware
from backend.api.solutions import router as solutions_router
from backend.core.config import get_settings
from backend.core.db import init_schema
from backend.services import auth_service

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：启动时建库、补初始数据。

    放在 lifespan 而不是 `@app.on_event` 的原因：后者已在新版本 FastAPI 中标记弃用。
    """
    init_schema()
    auth_service.ensure_bootstrap()
    # 开发模式下预先备好内置用户，业务数据落归属时才有恒非空的 user_id
    if not settings.auth_enabled:
        auth_service.ensure_dev_user()
    yield


app = FastAPI(
    title="HarnessPRD API",
    description="HarnessPRD 后端服务",
    version="0.1.0",
    lifespan=lifespan,
)

# 中间件注册顺序：Starlette 的 add_middleware 是「后加的在外层」。
# 因此先注册认证（内层），后注册 CORS（外层）——跨域预检请求由 CORS 直接应答，
# 不会误闯认证层；同时 CORS 会给认证拒绝的 401 也补上跨域头，前端才读得到错误详情。
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册业务路由（各自在 Router 上声明了完整 prefix）
app.include_router(auth_router)
app.include_router(conversation_router)
app.include_router(config_router)
app.include_router(solutions_router)


@app.get("/", summary="服务根路径")
async def root() -> dict:
    """返回服务基本信息，用于快速确认后端已启动。"""
    return {
        "name": "HarnessPRD API",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health", summary="健康检查")
async def health() -> dict:
    """健康检查接口，供容器探针或前端探活使用。"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=settings.host, port=settings.port, reload=True)
