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

另有一条例外：**排空期间**（进程收到终止信号、正在等在途任务收尾）`/readyz` 一律 503，
连两项检查都不跑——那时结论已经定了，见 `services/job_drain.py`。

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
from backend.services.job_drain import drain_controller
from backend.services.skill_registry import (
    audit_skills,
    describe_manifest,
    load_skill_artifacts,
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
    """探测「库现在能不能写」：借一条连接，拿写锁 + 一次内容不变的页写，然后回滚。

    为什么不是 `SELECT 1`：只读的库（文件权限、只读挂载、磁盘满后的半只读状态）
    读得出来、写不进去，而「现在能接活」恰恰要求写得进去——存不了方案的服务，
    「健康」二字没有意义。这就是它与 `_check_database()`（只探「可连接」）的分工：
    详情端点回答「库通不通」，就绪探针回答「写不写得进去」。

    为什么是 `BEGIN IMMEDIATE`（而不是建一张探针表再回滚）：
    在 pysqlite 上，SQLAlchemy 的 `conn.begin()` **不发 `BEGIN`**（驱动等到第一条
    写语句才隐式开事务）。于是「建探针表」那条 DDL 在 SQLite 眼里是一句**没有外层
    事务的独立写**，自己就提交了——后面那句 `rollback()` 只撤掉了 Python 侧的事务
    记账，表已经落库（评审实测）。结果是探针每 15 秒真建一次表、真拿一次写锁，
    还往 `sqlite_master` 里写东西；这与「探活不该有副作用」直接冲突。
    `BEGIN IMMEDIATE` 是 SQLite 的「我要写」声明：能拿到 RESERVED 锁说明此刻没有
    别的写者占着（拿不到会立刻报 `SQLITE_BUSY`，不必等满超时），结尾 `ROLLBACK`
    把这次事务整个撤掉，不留任何痕迹。

    为什么在写锁之外还要补一次页写：
    **SQLite 从 3.53 起会放行只读库的 `BEGIN IMMEDIATE`**（锁是文件级语义，不校验
    写权限；实测 `mode=ro` 连接上 `BEGIN IMMEDIATE` 成功、下一条写语句才报
    `attempt to write a readonly database`）。只拿锁会把「只读库」误判成 ready——
    那是最坏的一种假绿。所以紧接着做一次**内容不变**的页写：把 `application_id`
    写成它当前的值。可写库上它只是一次会被回滚的脏页（表清单、`freelist_count`、
    文件内容都不变）；只读库上它当场报 `SQLITE_READONLY`，探针如实转 503。
    选 `application_id` 而不是 `user_version`：后者是已删除的旧手写迁移链的标识，
    C9 守卫禁止生产代码里出现它——版本的唯一真源是 `alembic_version`。两者同在库头，
    `application_id` 本项目从不读，写回原值纯粹是为了触发一次写。
    """
    try:
        with get_engine().connect() as conn:
            transaction = conn.begin()
            try:
                # 走 Engine 的池：与业务请求借同一条通路，才能测出「业务会写不进去」的那种故障。
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                current = conn.exec_driver_sql("PRAGMA application_id").scalar()
                conn.exec_driver_sql(f"PRAGMA application_id = {int(current or 0)}")
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
    return {"ok": True, "detail": "可写（写锁已拿到，页写已回滚）"}


def _check_skills_loadable() -> dict:
    """探测「技能包现在能不能被生成管线加载」。

    与 `_check_skills()` 的区别是**读者不同**，不是详略之别：那边报的是「加载了哪些包、
    各自服务哪些产物」（一份给人看的清单），这边只回答「能不能加载」，并且
    **真的把资源内容读一遍**——`audit_skills()` 只查文件在不在，而编码坏了、文件被占住、
    内容读不动这些情况只有在读的时候才会暴露。探针回答的是「现在能不能接活」，
    不是「目录摆得对不对」。

    读资源走的是生成链路自己的入口 `load_skill_artifacts(kind)`（`document_service`
    取资源就是它），而不是在这里自己拼路径、自己读文件：探针要回答的是「管线拿不拿得到
    资源」，那就该问管线问的那个函数，否则「探针绿了、生成却退回内置提示词」这种
    分叉迟早会出现。`applies_to` 里声明的每个产物都会过一遍。
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
            for kind in manifest.applies_to:
                if load_skill_artifacts(kind) is None:
                    # 走到这里说明资源在这条连接上是坏的（缺失已被 audit_skills 拦下）：
                    # 编码、权限、文件被占用，都属于「现在接不了活」。
                    return {
                        "ok": False,
                        "detail": f"技能包 {manifest.name} 加载失败（产物 {kind}）",
                    }
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

    **排空期间一律 503**（issue #13）。这一句不是「顺手加的一条判断」：收到终止信号
    之后，进程还能活最多 30 秒（等任务跑完），这段时间里它**不能再接新活**。编排器
    不读日志，它只看这条探针——探针此时若还说「就绪」，反代会继续把生成请求送进来，
    「停止接收新任务」当场失效。

    排空期间**不再跑那两项依赖检查**：它们各要借一次连接、读一遍技能包，而此刻的
    结论已经定了（503）。少做一次写探针也顺带少一次「退出前把库锁住」的机会。
    """
    if drain_controller.is_draining():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "not_ready",
            "checks": {
                "draining": {
                    "ok": False,
                    "detail": "服务正在重启（排空），不再接收新任务",
                }
            },
        }

    checks = {
        "database": _check_database_writable(),
        "skills": _check_skills_loadable(),
    }
    ready = all(item["ok"] for item in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready else "not_ready", "checks": checks}
