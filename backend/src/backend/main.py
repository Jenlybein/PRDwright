"""FastAPI 应用入口。

本地启动（在 backend/ 目录下执行）：

    uv run uvicorn backend.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.auth import router as auth_router
from backend.api.config import router as config_router
from backend.api.conversation import router as conversation_router
from backend.api.deps import AuthMiddleware
from backend.api.jobs import router as jobs_router
from backend.api.solutions import router as solutions_router
from backend.core.config import get_settings
from backend.core.db import init_schema
from backend.core.errors import DEFAULT_STATUS, AppError, ErrorCode, error_payload
from backend.services import auth_service, job_service

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
    # 收上次进程残留的 running 生成任务：内存运行时已随进程消失，DB 里不可能还有真在跑的
    job_service.reap_orphans()
    yield


app = FastAPI(
    title="PRDwright API",
    description="PRDwright 后端服务",
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


async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    """把 `AppError` 渲染成 `{detail, code}`。

    异常来自 `api/` 与 `services/`，两者只负责抛出，渲染规则集中在这里，
    因此「错误码对应哪个状态码」这件事只有一处定义（见 `core/errors.py`）。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(exc.message, exc.code),
    )


async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Pydantic 校验失败：保留 FastAPI 原样的 `detail` 列表，并列补上 `code`。

    `detail` 的结构与措辞**照抄** FastAPI 默认行为（`jsonable_encoder` 是它内部
    用的同一个序列化器），前端才能继续按既有方式解析 422。
    """
    return JSONResponse(
        status_code=DEFAULT_STATUS[ErrorCode.VALIDATION_ERROR],
        content=error_payload(jsonable_encoder(exc.errors()), ErrorCode.VALIDATION_ERROR),
    )


def register_error_handlers(target: FastAPI) -> None:
    """给一个应用装上错误契约的处理器。

    抽成函数而不是直接挂在模块级 `app` 上，是因为**测试里的探针应用**也会用到它
    （见 `tests/test_auth_service.py` 的 `acting_user` 探针）：探针直接抛 `AppError`，
    不装处理器就会 500，装了才能验证到与生产一致的响应体。

    必须在路由注册之前调用：路由表一旦固化，「先声明契约、再挂业务」的阅读顺序
    就被打乱，容易让人误以为处理器是某几个路由的私有配置。

    为什么不能用 `HTTPException(detail={"detail": ..., "code": ...})`：FastAPI 会把
    `detail` 原样塞进响应体的 `detail` 字段，渲染成 `{"detail": {"detail": ...}}`
    的嵌套结构。要让两者并列，只能走自定义处理器。
    """
    target.add_exception_handler(AppError, handle_app_error)
    target.add_exception_handler(RequestValidationError, handle_validation_error)


register_error_handlers(app)


# 注册业务路由（各自在 Router 上声明了完整 prefix）
app.include_router(auth_router)
app.include_router(conversation_router)
app.include_router(config_router)
app.include_router(solutions_router)
app.include_router(jobs_router)


@app.get("/", summary="服务根路径")
async def root() -> dict:
    """返回服务基本信息，用于快速确认后端已启动。"""
    return {
        "name": "PRDwright API",
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
