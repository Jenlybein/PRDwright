"""FastAPI 应用入口。

本地启动（在 backend/ 目录下执行）：

    uv run uvicorn backend.main:app --reload
"""

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.admin import router as admin_router
from backend.api.auth import router as auth_router
from backend.api.config import router as config_router
from backend.api.conversation import router as conversation_router
from backend.api.deps import AuthMiddleware, RequestIdMiddleware
from backend.api.documents import router as documents_router
from backend.api.export import router as export_router
from backend.api.health import router as health_router
from backend.api.jobs import router as jobs_router
from backend.api.solutions import router as solutions_router
from backend.core.config import get_settings
from backend.core.runtime_config import get_runtime_config, invalidate
from backend.core.db import init_schema
from backend.core.errors import DEFAULT_STATUS, AppError, ErrorCode, error_payload
from backend.core.migrations import run_migrations
from backend.core.logging_setup import (
    configure_logging,
    install_log_persistence,
    shutdown_log_persistence,
)
from backend.services import auth_service, job_service
from backend.services.skill_registry import ensure_skills

logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：启动时建库、补初始数据。

    放在 lifespan 而不是 `@app.on_event` 的原因：后者已在新版本 FastAPI 中标记弃用。
    """
    # 日志配置放在最前面：连「建库失败」这件事也应当带上时间与来源，
    # 否则启动期的报错仍是难认的裸文本。这里重新取一次配置而不复用模块级
    # `settings` 快照——测试与 `--reload` 都可能在本模块导入之后才改环境变量。
    current = get_settings()
    configure_logging(level=current.log_level, fmt=current.log_format)

    init_schema()
    # 建表之后立刻补齐 schema 演进：`init_schema` 只保证基线表存在（`IF NOT EXISTS`
    # 对已存在的表是空操作），新增的表与列一律由迁移补上。顺序不能颠倒——迁移可能
    # 依赖基线表（例如把旧快照回填进新表）。
    run_migrations()
    # 迁移可能刚建好 `system_config` / `model_providers`，也可能刚播种了内置供应商。
    # 配置层的缓存若在迁移之前被建过（导入期、或某个模块级初始化），它会记住
    # 「表不存在 → 降级」那一份，并在恢复 TTL 内继续用降级值。这里主动失效一次，
    # 让第一条业务请求读到的是真实的配置。
    invalidate()
    # 日志持久化必须在建表与迁移**之后**挂上：`app_logs` 表若还不存在，
    # 启动这批日志会全部写库失败并降级，而「启动日志恰好没有」正是最影响排障、
    # 又最不容易被察觉的一种丢数据。放在这里还有个附带好处——后面每一步
    # （孤儿回收、技能自检、启动完成）的日志都被留下来。
    install_log_persistence()
    auth_service.ensure_bootstrap()
    # 开发模式下预先备好内置用户，业务数据落归属时才有恒非空的 user_id
    if not settings.auth_enabled:
        auth_service.ensure_dev_user()
    # 收上次进程残留的 running 生成任务：内存运行时已随进程消失，DB 里不可能还有真在跑的
    job_service.reap_orphans()
    # 技能资产自检：扫一遍 skills 根目录，把「元数据读不动 / 必需资源缺失 /
    # 同一产物被多个技能包声明」三类问题一次报出来。默认只告警（本地调试缺 skills
    # 也能起服务），设 PRDWRIGHT_SKILLS_STRICT=true 后存在问题即中止启动——部署态用。
    manifests, skill_problems = ensure_skills(strict=settings.skills_strict)
    if skill_problems:
        logger.warning(
            "技能资产自检未通过：%s。相关产物的生成将退回内置提示词（回执会标出）",
            "；".join(f"{item.skill} {item.reason}" for item in skill_problems),
            extra={"event": "service.skills_problem", "problem_count": len(skill_problems)},
        )

    # 一条启动记录，把「这次的进程究竟加载了哪套资产」钉在时间线上。
    # 排查「昨天还好、今天风格变了」这类问题时，第一眼要找的就是它。
    logger.info(
        "服务启动完成",
        extra={
            "event": "service.started",
            "log_format": current.log_format,
            # 记**实际生效**的供应商与来源：`source` 是 db / env / degraded 之一，
            # 一眼就能看出「后台改的配置到底有没有被读到」。排查「改了没生效」时
            # 这是第一眼要看的信息。
            "llm": _startup_llm_label(get_runtime_config()),
            "config_source": get_runtime_config().source,
            "skills": "、".join(
                f"{manifest.name} v{manifest.version}"
                f"（{'/'.join(manifest.applies_to) or '未声明产物'}）"
                for manifest in manifests
            )
            or "未发现技能包（生成将退回内置提示词）",
        },
    )
    yield

    # 退出前 flush：最后一批日志还可能在内存队列里，而它们往往正好是
    # 「服务为什么退出」的那几行。
    shutdown_log_persistence()


def _startup_llm_label(config) -> str:
    """启动日志里那截 `协议/模型` 文本。

    取**运行时生效**的供应商而不是 `DEFAULT_LLM_PROVIDER`：后台切过供应商之后，
    启动日志若还印着 env 里那家，读日志的人会以为配置没生效——而实际上生效了，
    只是这一行没跟上。
    """
    spec = config.active_provider
    if spec is None:
        return "（无可用供应商）"
    return f"{spec.protocol}/{spec.model or '（未填模型）'}"


app = FastAPI(
    title="PRDwright API",
    description="PRDwright 后端服务",
    version="0.1.0",
    lifespan=lifespan,
)

# 中间件注册顺序：Starlette 的 add_middleware 是「后加的在外层」。
# 因此先注册认证（内层），再注册 CORS，最后注册 request_id（最外层）——跨域预检请求
# 由 CORS 直接应答，不会误闯认证层；同时 CORS 会给认证拒绝的 401 也补上跨域头，
# 前端才读得到错误详情。request_id 放最外层，是为了让**认证拦下的 401 也带得上
# `X-Request-ID`**：报障时用户手上那个号必须存在于每一次响应里。
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestIdMiddleware)


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
# 管理员路由：整组由 `require_admin` 依赖守卫，未登录另有中间件拦成 401
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(conversation_router)
app.include_router(config_router)
app.include_router(solutions_router)
# 文档路由挂在 `/api/solutions/{solution_id}/documents`，与方案是子资源关系；
# 单独成文件是为了让「方案快照」与「产物正文」这两件生命周期不同的事各有归处。
app.include_router(documents_router)
app.include_router(export_router)
app.include_router(jobs_router)
# 健康检查路径为 `/health`（无前缀），与认证白名单里的条目必须一致
app.include_router(health_router)


@app.get("/", summary="服务根路径")
async def root() -> dict:
    """返回服务基本信息，用于快速确认后端已启动。"""
    return {
        "name": "PRDwright API",
        "status": "running",
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=settings.host, port=settings.port, reload=True)
