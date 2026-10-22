"""健康检查路由。

**它回答的问题**：这次「用不了」是哪一类？——不是「服务活着吗」（活着但没法用是常态）。

因此一次访问同时报三件事，它们正好覆盖了「明明起着却用不了」的全部常见原因：

- **数据库**：连不上就存不了方案，也创建不了任务；
- **大模型供应商**：密钥没配 / 配错供应商，生成必然失败；
- **技能资产**：技能包不在位 / 元数据读不动 / 必需资源缺失时都**不会报错**，只会悄悄换成
  内置兜底提示词（见 ADR-017）。这里按技能注册表逐个报出加载了哪些包、各自服务哪些产物。

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

from backend.core.db import connect
from backend.core.runtime_config import get_runtime_config, provider_key_present
from backend.services.skill_registry import audit_skills, describe_manifest, skills_root

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
    """探测大模型供应商：**只校验配置就位，不发真实调用**（见模块文档）。

    读的是**运行时配置**而不是 env：后台切过供应商之后，体检必须反映「实际生效的
    那一家」。否则会出现「体检说 ark 正常、实际生成却在用另一家」的假绿。
    """
    config = get_runtime_config()
    spec = config.active_provider
    if spec is None:
        return {
            "ok": False,
            "detail": "还没有任何模型配置，请先在管理后台新增一条",
            "provider": "",
            "model": "",
            "config_source": config.source,
        }

    provider = spec.protocol
    model = spec.model

    if not provider_key_present(spec):
        # 不用异常自带的消息：那句里带 `.env` 的绝对路径，而本接口是公开的。
        # 变量名本身可以给——它不含密钥值，只是「去哪个环境变量取」。
        return {
            "ok": False,
            "detail": "未配置密钥（请在管理后台「模型配置」页填入）",
            "provider": provider,
            "model": model,
            "config_source": config.source,
        }

    return {
        "ok": True,
        "detail": f"{provider} / {model}（仅校验配置，未发起真实调用）",
        "provider": provider,
        "model": model,
    }


def _check_skills() -> dict:
    """探测技能资产：报出「加载了哪些包、各自服务哪些产物」，以及全部问题。

    上一版只认 PRD 那一个技能包；现在按**技能注册表**报（见
    `services/skill_registry.py`），所以新增技能包不必回来改体检——
    它自己声明 `applies_to` 就会出现在这里。
    """
    manifests, problems = audit_skills()
    if problems:
        return {
            "ok": False,
            "detail": "；".join(f"{item.skill} {item.reason}" for item in problems),
        }
    if not manifests:
        # 一个包都没扫到：多为部署时漏打包 skills/ 目录，或本地没配 PRDWRIGHT_SKILLS_DIR
        return {
            "ok": False,
            "detail": f"未发现任何技能包（skills 根目录 {skills_root()} 为空或未打包）",
        }

    entries = []
    for manifest in manifests:
        # `audit_skills` 已确认资源齐全，这里再取一次身份是为了拿指纹——
        # 它与回执上那个指纹同源，能直接对上「这份文档是按哪一套写的」。
        fingerprint = (describe_manifest(manifest) or {}).get("fingerprint", "")
        entries.append(
            {
                "name": manifest.name,
                "version": manifest.version,
                "fingerprint": fingerprint,
                "applies_to": list(manifest.applies_to),
            }
        )

    return {
        "ok": True,
        "detail": "；".join(
            f"{item['name']} v{item['version']} · {item['fingerprint']}"
            f"（{'/'.join(item['applies_to']) or '未声明产物'}）"
            for item in entries
        ),
        "skills": entries,
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
