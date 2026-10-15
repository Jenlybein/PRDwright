"""健康检查路由。

**它回答的问题**：这次「用不了」是哪一类？——不是「服务活着吗」（活着但没法用是常态）。

因此一次访问同时报三件事，它们正好覆盖了「明明起着却用不了」的全部常见原因：

- **数据库**：连不上就存不了方案，也创建不了任务；
- **大模型供应商**：密钥没配 / 配错供应商，生成必然失败；
- **技能包**：文件不在位时不会报错，只会悄悄换成内置兜底提示词（见 ADR-017）。

**`ok` 的语义要说清**：它表示「配置就位 / 依赖可达」，不是「功能已经验证过」。
尤其是供应商这一项——这里**只校验密钥与型号是否配置**，不发真实调用：
健康检查可能被容器探针高频触发，让它按次烧额度不合适；而真到了连不上、额度耗尽，
那属于调用时的错误，`stream_reply` 的失败路径已经会给出明确错误码与文案。
**不假装做过检查**，所以 `detail` 里会写明「仅校验配置，未发起真实调用」。

**备用供应商**：ADR-016 已裁决不做运行时自动 failover，所以这里只报当前那一个。
写「主/备可用性」的初衷是想在切换前预警，既然切换本身取消了，预警也就没有对象。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from backend.core.config import get_settings
from backend.core.db import connect
from backend.services.prd_skill import describe_skill, missing_skill_assets

router = APIRouter(tags=["system"])

logger = logging.getLogger(__name__)


def _check_database() -> dict:
    """探测数据库：借一条连接执行最小查询。

    刻意用「真的连一次」而不是看文件在不在——SQLite 的问题大多不是文件缺失，
    而是权限、目录、WAL 文件被占用这类只有连上去才会暴露的情况。
    """
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - 体检要兜住任何异常，否则「探活」自己就 500 了
        logger.warning(
            "健康检查：数据库不可用",
            extra={"event": "health.database_unavailable", "error_type": type(exc).__name__},
            exc_info=True,
        )
        # 只留异常类型，不带消息与路径：`/health` 是公开路径，不往外吐内部细节
        return {"ok": False, "detail": f"数据库不可用（{type(exc).__name__}）"}
    return {"ok": True, "detail": "可连接"}


def _check_llm() -> dict:
    """探测大模型供应商：**只校验配置就位，不发真实调用**（见模块文档）。"""
    settings = get_settings()
    provider = settings.default_llm_provider
    model = settings.default_llm_model

    try:
        settings.require_api_key()
    except RuntimeError:
        # 不用异常自带的消息：那句里带 `.env` 的绝对路径，而本接口是公开的
        return {
            "ok": False,
            "detail": f"未配置 {settings.api_key_env_var()}",
            "provider": provider,
            "model": model,
        }

    return {
        "ok": True,
        "detail": f"{provider} / {model}（仅校验配置，未发起真实调用）",
        "provider": provider,
        "model": model,
    }


def _check_skills() -> dict:
    """探测技能包：缺失时报出缺哪几个文件，而不是让它在生成时静默降级。"""
    missing = missing_skill_assets()
    if missing:
        return {"ok": False, "detail": "缺少技能资源：" + "、".join(missing)}

    identity = describe_skill()
    if identity is None:
        # `missing_skill_assets` 说齐、身份却取不到：属于资源存在但读不动的边界情况
        return {"ok": False, "detail": "技能资源存在但无法读取"}

    return {
        "ok": True,
        "detail": f"{identity['name']} v{identity['version']} · {identity['fingerprint']}",
        "name": identity["name"],
        "version": identity["version"],
        "fingerprint": identity["fingerprint"],
    }


@router.get("/health", summary="健康检查")
async def health() -> dict:
    """返回三项体检结果。

    `status` 只分两档：三项全 `ok` 是 `ok`，任一不过就是 `degraded`。
    **刻意不返回非 200**：技能包缺失时服务仍可降级运行，把状态码打成失败会让
    编排器把还能用的容器踢掉；反过来数据库挂了也不该靠状态码表达——
    调用方（人、脚本、探针）看 `status` 与 `checks` 就能分清，这正是本接口存在的意义。
    """
    checks = {
        "database": _check_database(),
        "llm": _check_llm(),
        "skills": _check_skills(),
    }
    status = "ok" if all(item["ok"] for item in checks.values()) else "degraded"
    return {"status": status, "checks": checks}
