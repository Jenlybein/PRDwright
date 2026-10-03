"""FastAPI 应用入口。

本地启动（在 backend/ 目录下执行）：

    uv run uvicorn backend.main:app --reload
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.config import router as config_router
from backend.api.conversation import router as conversation_router
from backend.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title="HarnessPRD API",
    description="HarnessPRD 后端服务",
    version="0.1.0",
)

# 跨域配置：允许的来源来自 .env 中的 CORS_ORIGINS（多个用逗号分隔）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册业务路由（各自在 Router 上声明了完整 prefix）
app.include_router(conversation_router)
app.include_router(config_router)


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
