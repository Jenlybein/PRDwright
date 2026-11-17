"""部署面守卫（v9 / #78）：部署件不许再绑定已删除的后端。

为什么部署面值得一组**静态**守卫：代码里的 SQLite / MySQL 分支删掉之后，测试全绿
并不能证明部署面也换过来了——`deploy/` 下的 compose、Dockerfile、启动脚本与备份脚本
不在 `backend/tests` 的运行路径里，它们的错误要到 CI 的 `container-smoke`（真起整栈）
才会暴露，而那条链路一次要跑十几分钟。

这里读的就是仓库里的**实际部署件**（不是「另一个描述它们的文档」），判据是事实：

1. compose 有独立 `db` 服务、`api` 等它**健康**、数据在 `pgdata` 卷、api 的
   `DATABASE_URL` 由编排层按凭据拼出来（镜像里没有可用口令）；
2. 探针用的是 `/readyz`，不是恒 200 的 `/healthz`；
3. 镜像默认 `DATABASE_URL` 是 Postgres 形态且不含口令；
4. 启动脚本不再解析 SQLite 路径，而是等数据库就绪；
5. 备份脚本走 `pg_dump` / `pg_restore`，不再 import `sqlite3`、不再有「固定 SQLite」的判断。
"""

from __future__ import annotations

import ast
import pathlib

import pytest
import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DEPLOY = _REPO_ROOT / "deploy"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(_read(_DEPLOY / "compose.yaml"))


def test_compose_runs_a_postgres_service(compose):
    """数据库是独立服务，而不是 api 容器里的一个文件。"""
    services = compose["services"]
    assert "db" in services, "compose 里没有 db 服务（数据库不再是独立服务了？）"
    db = services["db"]
    assert db["image"].startswith("postgres:"), "db 服务必须用官方 Postgres 镜像"
    # 凭据从 .env 来，且**缺失时直接报错**（不提供默认口令）
    assert "POSTGRES_PASSWORD" in db["environment"]
    assert ":?" in db["environment"]["POSTGRES_PASSWORD"], "口令缺失时必须让 compose 报错"
    assert any("pgdata" in item for item in db["volumes"]), "数据库数据卷必须是命名卷 pgdata"
    assert "pg_isready" in db["healthcheck"]["test"][-1], "健康判据要基于 pg_isready"


def test_api_waits_for_a_healthy_database(compose):
    """`depends_on` 必须是「等它健康」，不是「启动顺序」。"""
    api = compose["services"]["api"]
    depends = api.get("depends_on")
    assert isinstance(depends, dict), "api 的 depends_on 必须是带 condition 的映射"
    assert depends["db"]["condition"] == "service_healthy"
    # 数据库地址由编排层拼：服务名 db + 5432
    database_url = api["environment"]["DATABASE_URL"]
    assert database_url.startswith("postgresql+psycopg://")
    assert "@db:5432/" in database_url


def test_healthcheck_uses_readiness_probe(compose):
    """健康检查必须用 `/readyz`：`/healthz` 恒 200，拿它探活等于没配。"""
    test = compose["services"]["api"]["healthcheck"]["test"]
    joined = " ".join(test)
    assert "/readyz" in joined
    assert "/healthz" not in joined


@pytest.mark.parametrize("needle", ["SQLite", "sqlite", "VACUUM", "prdwright_data"])
def test_compose_has_no_removed_backend_or_old_volume(compose, needle):
    """编排里不许再出现已删除的后端与旧的数据卷。"""
    text = _read(_DEPLOY / "compose.yaml")
    # `pgdata` 的说明里会提到旧卷名（历史说明允许写出来），按行判：只看非注释行
    code_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    offenders = [line for line in code_lines if needle in line]
    assert offenders == [], f"compose 的非注释行里还有 {needle!r}：{offenders}"


def test_api_image_default_database_url_is_postgres_without_password():
    """镜像里的默认地址必须是 Postgres 形态，且**不含口令**。

    内置一个含口令的默认值等于把凭据写进镜像层；内置一个 SQLite 路径则是回退到已删除的
    后端。判据落在 ENV 那一行本身：`postgresql+psycopg://user@host:port/db` 形态即没有
    口令（`user:password` 的 `:` 不会出现在 `@` 之前）。
    """
    text = _read(_DEPLOY / "docker" / "api.Dockerfile")
    line = next(
        (line.strip() for line in text.splitlines() if "DATABASE_URL=" in line),
        "",
    )
    assert line, "api.Dockerfile 里没有 DATABASE_URL"
    value = line.split("DATABASE_URL=", 1)[1].rstrip(" \\")
    assert value.startswith("postgresql+psycopg://"), f"默认地址不是 Postgres：{value}"
    userinfo = value.split("://", 1)[1].split("@", 1)[0]
    assert ":" not in userinfo, f"镜像默认地址里带了口令：{value}"


def test_entrypoint_waits_for_database_and_has_no_sqlite_branch():
    """启动脚本不再解析 SQLite 路径，而是等数据库就绪。"""
    text = _read(_DEPLOY / "docker" / "api-entrypoint.sh")
    assert "wait_for_database" in text
    assert "sqlite*:///" not in text, "启动脚本还在解析 SQLite 路径"
    assert "PRDWRIGHT_DATA_DIR" not in text
    assert "psycopg" in text, "等数据库就绪应复用镜像里的 psycopg，而不是装额外的客户端"


def _backup_module() -> ast.Module:
    return ast.parse(_read(_DEPLOY / "backup.py"), filename="deploy/backup.py")


def test_backup_script_uses_pg_dump_not_sqlite():
    """备份脚本走 `pg_dump` / `pg_restore`，且不再 import `sqlite3`。"""
    tree = _backup_module()
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "sqlite3" not in imported, "backup.py 还在直接操作 SQLite"

    text = _read(_DEPLOY / "backup.py")
    assert "pg_dump" in text, "备份必须走 pg_dump"
    assert "pg_restore" in text, "自检必须能解析归档（pg_restore --list）"
    assert "固定使用 SQLite" not in text, "「本项目固定使用 SQLite」的判断必须删掉"


def test_backup_script_rejects_non_postgres_database_url():
    """`DATABASE_URL` 指向别的后端时明确拒绝，而不是猜一个降级流程。"""
    text = _read(_DEPLOY / "backup.py")
    assert "只支持 Postgres" in text
    assert "sqlite" in text.lower(), "报错里应当点明被拒绝的后端"
