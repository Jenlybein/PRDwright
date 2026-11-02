"""健康检查路由：一个详情端点 + 两条探针。

**三个端点，三种读者**——先把这个分工写死，混用会出事故：

| 端点 | 回答的问题 | 读者 | 状态码 |
| --- | --- | --- | --- |
| `GET /health` | 这次「用不了」是哪一类？ | 人（排障、后台面板） | **恒 200** |
| `GET /healthz` | 这个进程还活着吗？ | 编排器 liveness | 恒 200 |
| `GET /readyz` | 现在能接活吗？ | 编排器 readiness | 200 / 503 |

`/health` 与 `/readyz` 的差别不是「谁查得多」，而是**读者不同**：体检报告是给人看的，
它的价值在于说清「哪一类不可用」，所以它是三档细节、恒 200；编排器只认两档结论——
200 就调度、503 就摘流或重启。把详情端点当探针用，等于让编排器去读一份「三项全红也恒
200」的报告，那和没配探针是一样的（v8 之前正是这个状态，见 issue #12）。

`/readyz` 只查两件事：**库可写**、**技能包可加载**。刻意不查模型供应商——那一项要发外部
请求，而探针会被高频触发（理由见下面对供应商一项的说明）。已知边界：供应商配置坏了，
`/readyz` 仍然说「就绪」，那是 `/health` 的职责。

## `/health`：给人看的体检报告

**它回答的问题**：这次「用不了」是哪一类？——不是「服务活着吗」（活着但没法用是常态）。

因此一次访问同时报三件事，它们正好覆盖了「明明起着却用不了」的全部常见原因：

- **数据库**：连不上就存不了方案，也创建不了任务；
- **大模型供应商**：密钥没配 / 配错供应商，生成必然失败；
- **技能资产**：技能包不在位 / 元数据读不动 / 必需资源缺失时都**不会报错**，只会悄悄换成
  内置兜底提示词。这里按技能注册表逐个报出加载了哪些包、各自服务哪些产物。

**`ok` 的语义要说清**：它表示「配置就位 / 依赖可达」，不是「功能已经验证过」。
尤其是供应商这一项——这里**只校验密钥与型号是否配置**，不发真实调用：
健康检查可能被容器探针高频触发，让它按次烧额度不合适；而真到了连不上、额度耗尽，
那属于调用时的错误，`stream_reply` 的失败路径已经会给出明确错误码与文案。
**不假装做过检查**，所以 `detail` 里会写明「仅校验配置，未发起真实调用」。

**备用供应商**：已裁决不做运行时自动 failover，所以这里只报当前那一个。
写「主/备可用性」的初衷是想在切换前预警，既然切换本身取消了，预警也就没有对象。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from backend.core.engine import get_engine
from backend.core.runtime_config import get_runtime_config, provider_key_present
from backend.services.skill_registry import (
    audit_skills,
    describe_manifest,
    read_text_cached,
    skills_root,
)

router = APIRouter(tags=["system"])

logger = logging.getLogger(__name__)


def _check_database() -> dict:
    """探测数据库：借一条连接执行最小查询。

    刻意用「真的连一次」而不是看文件在不在——SQLite 的问题大多不是文件缺失，
    而是权限、目录、WAL 文件被占用这类只有连上去才会暴露的情况。
    """
    try:
        # 走 Engine 的池：与业务请求借同一条通路，才能测出「业务真的会用不了的」那种故障。
        # 直接 `sqlite3.connect(文件)` 会绕过池与 PRAGMA 事件，探活绿着、业务却挂。
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
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


# ---------------------------------------------------------------------------
# 探针：生死只管「活着」，就绪只管「能接活」
# ---------------------------------------------------------------------------
#
# 两条探针的判据刻意**互不重叠**，也不要互相复用：
# 生死探针一旦开始查依赖，库一坏编排器就会重启容器——而重启治不了坏库，只会把
# 一个还能降级服务、还能让人登进去看日志的进程反复杀掉。反过来，就绪探针一旦只查
# 「进程活着」，它就退化成了一句口语，摘流与放量都没有依据。


def _check_database_writable() -> dict:
    """探测「库现在能不能写」：借一条连接，建一张探针表再删掉，全程回滚。

    为什么不是 `SELECT 1`：只读的库（文件权限、只读挂载、磁盘满后的半只读状态）
    读得出来、写不进去，而「现在能接活」恰恰要求写得进去——存不了方案的服务，
    「健康」二字没有意义。这就是它与 `_check_database()`（只探「可连接」）的分工：
    详情端点回答「库通不通」，就绪探针回答「写不写得进去」。

    为什么敢建表：整段包在一个事务里，结尾**无条件回滚**。SQLite 支持事务内的 DDL 回滚；
    换到 MySQL 时 DDL 会隐式提交，但探针表建完就删，落库的东西同样是零。
    代价只是一次极短的写锁，在这个探针的触发频率下可以忽略。
    """
    try:
        with get_engine().connect() as conn:
            transaction = conn.begin()
            try:
                # 走 Engine 的池：与业务请求借同一条通路，才能测出「业务会写不进去」的那种故障。
                conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS __readyz_probe (id INTEGER)")
                conn.exec_driver_sql("DROP TABLE IF EXISTS __readyz_probe")
            finally:
                transaction.rollback()
    except Exception as exc:  # noqa: BLE001 - 探针要兜住任何异常，否则它自己就 500 了
        logger.warning(
            "就绪探针：数据库不可写",
            extra={"event": "readyz.database_unwritable", "error_type": type(exc).__name__},
            exc_info=True,
        )
        # 只留异常类型：探针和 `/health` 一样是公开路径，不往外吐路径等内部细节
        return {"ok": False, "detail": f"数据库不可写（{type(exc).__name__}）"}
    return {"ok": True, "detail": "可写（探针临时表已回滚）"}


def _check_skills_loadable() -> dict:
    """探测「技能包现在能不能被生成管线加载」。

    与 `_check_skills()` 的区别是**读者不同**，不是详略之别：那边报的是「加载了哪些包、
    各自服务哪些产物」（一份给人看的清单），这边只回答「能不能加载」，并且
    **真的把资源内容读一遍**——`audit_skills()` 只查文件在不在，而编码坏了、文件被占住、
    内容读不动这些情况只有在读的时候才会暴露。探针回答的是「现在能不能接活」，
    不是「目录摆得对不对」。
    """
    try:
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
        for manifest in manifests:
            for key in manifest.artifacts:
                read_text_cached(manifest.artifact_path(key))
    except Exception as exc:  # noqa: BLE001 - 同上：探针不许自己 500
        logger.warning(
            "就绪探针：技能包加载失败",
            extra={"event": "readyz.skills_unloadable", "error_type": type(exc).__name__},
            exc_info=True,
        )
        return {"ok": False, "detail": f"技能包加载失败（{type(exc).__name__}）"}

    return {
        "ok": True,
        "detail": "可加载：" + "、".join(f"{item.name} v{item.version}" for item in manifests),
    }


@router.get("/healthz", summary="生死探针")
async def healthz() -> dict:
    """恒 200：只表示「进程活着、事件循环还能应答」。

    **不查任何依赖**。库坏了、技能包丢了、模型没配，这里照样 200——那不是它该回答的问题
    （见模块文档里的三端点分工）。响应体刻意只有 `{"ok": true}`：它被编排器高频调用，
    每多一个字段就多一次「有人拿它判断业务」的机会。
    """
    return {"ok": True}


@router.get("/readyz", summary="就绪探针", responses={503: {"description": "有依赖不可用"}})
async def readyz(response: Response) -> dict:
    """两项都正常返回 200；任一失败返回 503，并在 `checks` 里指出是哪一项、为什么。

    `status` 用 `ready` / `not_ready` 而不是详情端点那套 `ok` / `degraded`：
    两条端点的结论语义不同，字段名一样反而容易被混着用。
    """
    checks = {
        "database": _check_database_writable(),
        "skills": _check_skills_loadable(),
    }
    ready = all(item["ok"] for item in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready else "not_ready", "checks": checks}
