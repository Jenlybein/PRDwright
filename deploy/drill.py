#!/usr/bin/env python3
"""PRDwright 回滚预演练：备份 → 破坏 → 恢复，全程在**本机开发库**上跑一遍。

## 这是什么，不是什么

这是 `deploy/README.md` §6 与 `deploy/ROLLBACK.md` 那条「先备份、再造数据、再
`pg_dump -Fc`、再砸库、再 `pg_restore`」链路的**可重复执行版本**：靠它证明「备份
真的能恢复」，并把每一步的实际耗时记下来。它不是生产演练，也不压测：本机
Postgres（默认 `127.0.0.1:5433`）一个 15 表的小库，几十毫秒跑完一步。

## 安全性（为什么它不可能碰生产卷）

三条硬闸，任何一条不满足就直接退出、什么都不改：

1. **只能连本机**：DSN 的主机必须是 `127.0.0.1` / `localhost` / `::1`。要连别的
   机器得显式设 `PRDWRIGHT_DRILL_ALLOW_REMOTE=1`（默认拒绝，避免「把演练跑在线上」）。
2. **只动一个写死的库名**：`prdwright_drill`。库名是常量，不接受命令行覆盖，
   所以「drop database」的对象永远不可能是 `prdwright`。
3. **不落 `deploy/backups/`**：备份产物写在系统临时目录，演练结束连同临时目录一起删；
   跑完 `git status` 必须干净。脚本末尾还会核对 `deploy/backups/` 的文件清单没变。

演练库是**每次从头建**的（`DROP DATABASE IF EXISTS … WITH (FORCE)` → `CREATE DATABASE`
→ 跑迁移到 head），所以重复跑得到的结果完全一致，不留状态。

## 用法

在 `deploy/` 下：

```
make drill
# 或展开成
uv run --project ../backend python drill.py
```

本机集成库不是缺省地址时用 `--dsn`（维护库地址，**不带库名**）：

```
uv run --project ../backend python drill.py \
  --dsn postgresql+psycopg://postgres:postgres@127.0.0.1:5433
```

演练结束后想看现场就不清理（只该在排查时用）：

```
uv run --project ../backend python drill.py --keep
```

## 前置条件

- Docker 可用，且有与服务端同主版本的客户端镜像（默认 `postgres:16`，与
  `deploy/compose.yaml` 的 db 镜像同 tag）。`pg_dump` / `pg_restore` 在**客户端容器里**
  跑，宿主机不需要装 Postgres 工具（与 `deploy/backup.py` 同一口径）。
- 本机 Postgres 起着，`postgres` 用户能建库/删库（开发集成库的常规形态）。

> 默认 DSN 里的 `postgres:postgres` 是**本机开发集成库的口令（仅本地）**，不是任何部署
> 的口令；生产凭据只从 `deploy/.env` 来，永远不要写进本文档或脚本。

## 退出码

| 退出码 | 失败在哪一步 |
| --- | --- |
| `2` | 预检：docker / 客户端镜像 / 本地库地址 / 库不可达 |
| `3` | 重建演练库或跑迁移 |
| `4` | 造数据、`pg_dump` 导出或归档自检 |
| `5` | 破坏库、`pg_restore` 恢复 |
| `6` | 恢复后的校验（表数 / 版本 / 标记行对不上） |
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy"
BACKUP_DIR = DEPLOY_DIR / "backups"

# 演练库名是常量，不接受覆盖：drop 的对象因此永远不可能是生产库。
DRILL_DATABASE = "prdwright_drill"
MAINTENANCE_DATABASE = "postgres"

# 维护库地址：不带库名（与 backend/tests/conftest.py 的集成库口径一致）。
DEFAULT_DSN = "postgresql+psycopg://postgres:postgres@127.0.0.1:5433"

DEFAULT_PG_TAG = "16"
CONTAINER_BACKUP_DIR = "/backup"
ARCHIVE_NAME = "prdwright-drill.dump"
DOCKER_TIMEOUT = 300

# 造的数据：三行 system_config，键名带 `drill-` 前缀，方便肉眼确认「恢复的是备份里的那份」。
SEED_ROWS: tuple[tuple[str, str], ...] = (
    ("drill-a", "alpha"),
    ("drill-b", "beta"),
    ("drill-c", "gamma"),
)
SEED_UPDATED_AT = "2026-11-17T00:00:00Z"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PRECHECK = 2
EXIT_SCHEMA = 3
EXIT_BACKUP = 4
EXIT_RESTORE = 5
EXIT_VERIFY = 6

STEP_NAMES = {
    EXIT_PRECHECK: "预检",
    EXIT_SCHEMA: "建库与迁移",
    EXIT_BACKUP: "备份",
    EXIT_RESTORE: "破坏与恢复",
    EXIT_VERIFY: "恢复后校验",
}


class DrillError(Exception):
    """带「失败在哪一步 + 下一步怎么办」的演练错误。"""

    def __init__(self, code: int, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _configure_stdio() -> None:
    """切到 UTF-8：Windows 控制台默认 GBK，而这里有 ✓ / → 这类字符。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            continue


def _run(cmd: list[str], timeout: int = DOCKER_TIMEOUT) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise DrillError(
            EXIT_PRECHECK,
            f"命令不存在：{cmd[0]}",
            "演练靠 docker CLI 跑 pg_dump / pg_restore；先装 Docker 并确认 `docker version` 可用。",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DrillError(
            EXIT_PRECHECK,
            f"命令超时（{timeout}s）：{' '.join(cmd[:6])} …",
            "数据库或 docker 卡住了。先 `docker ps` 看看 Postgres 是否健康。",
        ) from exc


def _tail(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fmt_seconds(seconds: float) -> str:
    return f"{seconds * 1000:.0f}ms" if seconds < 1 else f"{seconds:.1f}s"


@dataclass
class StepClock:
    """记录每一步的耗时；成功与否都记账（失败时前面几步的耗时照样可读）。"""

    steps: list[tuple[str, float, bool]] = field(default_factory=list)
    total: float = 0.0

    def record(self, name: str, seconds: float, ok: bool = True) -> None:
        self.steps.append((name, seconds, ok))
        print(f"[演练] {'✓' if ok else '✗'} {name:<28} {_fmt_seconds(seconds)}")


class _Timer:
    def __init__(self) -> None:
        self.started = time.perf_counter()

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started


# ---------------------------------------------------------------------------
# 连接与 SQL（psycopg 走 backend 的 venv，演练本身不新增依赖）
# ---------------------------------------------------------------------------


def _parse_dsn(dsn: str):
    """把 SQLAlchemy 风格的维护库地址（不带库名）解析成 psycopg 参数。"""
    try:
        from sqlalchemy.engine import make_url
    except ImportError as exc:  # pragma: no cover - 用 uv run 时不会走到
        raise DrillError(
            EXIT_PRECHECK,
            "缺 SQLAlchemy（不能用系统 python 直接跑这个脚本）。",
            "用 `uv run --project ../backend python drill.py` 或 `make drill`。",
        ) from exc

    try:
        url = make_url(dsn)
    except Exception as exc:
        raise DrillError(
            EXIT_PRECHECK,
            f"`--dsn` 解析不了：{dsn}",
            "形式：postgresql+psycopg://<用户>:<口令>@<主机>:<端口>（不带库名）。",
        ) from exc

    if url.get_backend_name() != "postgresql":
        raise DrillError(
            EXIT_PRECHECK,
            f"`--dsn` 不是 Postgres 地址：{url.drivername}",
            "本项目只支持 Postgres（v9 单后端）；SQLite / MySQL 一律拒绝。",
        )
    host = url.host or "127.0.0.1"
    if host not in LOOPBACK_HOSTS and os.environ.get("PRDWRIGHT_DRILL_ALLOW_REMOTE") != "1":
        raise DrillError(
            EXIT_PRECHECK,
            f"拒绝在非本机地址上演练：{host}",
            "演练会 drop/create 库。确实要连外部开发库时，显式设 "
            "PRDWRIGHT_DRILL_ALLOW_REMOTE=1（生产库永远不要这么做）。",
        )
    return url


def _psycopg_kwargs(url) -> dict[str, object]:
    return {
        "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
        "user": url.username,
        "password": url.password,
        "connect_timeout": 5,
    }


def _connect(kwargs: dict[str, object], database: str):
    import psycopg

    try:
        return psycopg.connect(**{**kwargs, "dbname": database}, autocommit=True)
    except psycopg.OperationalError as exc:
        raise DrillError(
            EXIT_PRECHECK,
            f"连不上 Postgres（库 {database}）：{exc}",
            "本机集成库没起？`docker ps` 里应有 5433 端口的那只 Postgres；"
            "或把 `--dsn` 指向真实地址。",
        ) from exc


# ---------------------------------------------------------------------------
# 各步骤
# ---------------------------------------------------------------------------


def _precheck(pg_image: str) -> None:
    _run(["docker", "version", "--format", "{{.Server.Version}}"])
    result = _run(["docker", "image", "inspect", pg_image, "--format", "{{.Id}}"], timeout=60)
    if result.returncode != 0:
        raise DrillError(
            EXIT_PRECHECK,
            f"客户端镜像不存在：{pg_image}",
            f"先拉一次：`docker pull {pg_image}`（与 compose 的 db 镜像同主版本）。",
        )


def _reset_database(maint_kwargs: dict[str, object]) -> None:
    """重建演练库：先 FORCE drop（踢掉残留连接），再建一个空库。"""
    from psycopg import sql

    with _connect(maint_kwargs, MAINTENANCE_DATABASE) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(DRILL_DATABASE)
            )
        )
        conn.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DRILL_DATABASE))
        )


def _migrate(drill_url: str) -> str:
    """走应用自己的迁移入口（`core/schema.py::prepare_schema`），与真实启动同一条路。"""
    # 让应用配置也指向演练库：prepare_schema 的目标由 engine 给出，但 settings 里
    # 顺手对齐，避免任何「默认库」的路径被意外启用。
    os.environ["DATABASE_URL"] = drill_url

    from backend.core.database import build_engine
    from backend.core.schema import prepare_schema
    from backend.core.engine import reset_engine

    reset_engine()
    engine = build_engine(drill_url)
    try:
        revision = prepare_schema(engine)
    finally:
        engine.dispose()
        reset_engine()
    return revision


def _seed(drill_kwargs: dict[str, object]) -> int:
    """往 system_config 写三行标记数据；返回该表行数（恢复后要靠它比对）。"""
    import psycopg

    conn = _connect(drill_kwargs, DRILL_DATABASE)
    try:
        with conn.cursor() as cursor:
            for key, value in SEED_ROWS:
                cursor.execute(
                    "INSERT INTO system_config (key, value, updated_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (key, value, SEED_UPDATED_AT),
                )
        return _count_seed_rows(conn)
    finally:
        conn.close()


def _count_seed_rows(conn) -> int:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM system_config WHERE key LIKE 'drill-%'"
        )
        return cursor.fetchone()[0]


def _live_state(conn) -> dict[str, object]:
    """破坏前记下「水印」：表数、迁移版本、标记行及其值。恢复后逐项比对。"""
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        tables = cursor.fetchone()[0]
        cursor.execute("SELECT version_num FROM alembic_version")
        revision = cursor.fetchone()[0]
        cursor.execute("SELECT key, value FROM system_config WHERE key LIKE 'drill-%' ORDER BY key")
        rows = [tuple(row) for row in cursor.fetchall()]
    return {"tables": tables, "revision": revision, "rows": rows}


def _container_host(host: str) -> str:
    """把「宿主上的本机地址」翻译成容器里看得到宿主的地址。

    演练库里 `pg_dump` / `pg_restore` 跑在一次性的客户端容器里：容器内的
    `127.0.0.1` 是**容器自己**，连不到宿主上的 5433。所以本机地址要换成
    `host.docker.internal`（docker run 时已加 `--add-host`，Linux 上也成立）。
    非本机地址（显式放开远端时）原样返回。
    """
    return "host.docker.internal" if host in LOOPBACK_HOSTS else host


def _dump(archive: Path, pg_image: str, dsn_kwargs: dict[str, object], database: str) -> None:
    """在一次性客户端容器里跑 `pg_dump -Fc`，产物经 bind mount 落回宿主临时目录。"""
    env_flags = [
        "-e",
        f"PGPASSWORD={dsn_kwargs['password']}",
    ]
    command = (
        [
            "docker",
            "run",
            "--rm",
            "--add-host=host.docker.internal:host-gateway",
            *env_flags,
            "-v",
            f"{archive.parent}:/backup",
            pg_image,
            "pg_dump",
            "-h",
            _container_host(str(dsn_kwargs["host"])),
            "-p",
            str(dsn_kwargs["port"]),
            "-U",
            str(dsn_kwargs["user"]),
            "-Fc",
            "-f",
            f"{CONTAINER_BACKUP_DIR}/{archive.name}",
            "-d",
            database,
        ]
    )
    result = _run(command)
    if result.returncode != 0:
        raise DrillError(
            EXIT_BACKUP,
            "`pg_dump -Fc` 失败。",
            f"pg_dump 的原话：\n{_tail(result.stderr) or '（无输出）'}",
        )
    if not archive.is_file() or archive.stat().st_size == 0:
        raise DrillError(
            EXIT_BACKUP,
            "备份产物没有落盘（0 字节或不存在）。",
            "检查宿主临时目录的磁盘空间与 Docker 的 bind mount 权限。",
        )


def _verify_archive(archive: Path, pg_image: str, expected_tables: int) -> int:
    """用 `pg_restore --list` 解析归档（不连库），返回 `TABLE DATA` 条目数。"""
    command = (
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{archive.parent}:/backup:ro",
            pg_image,
            "pg_restore",
            "--list",
            f"{CONTAINER_BACKUP_DIR}/{archive.name}",
        ]
    )
    result = _run(command, timeout=180)
    if result.returncode != 0:
        raise DrillError(
            EXIT_BACKUP,
            "`pg_restore --list` 解析不了这份归档（备份不完整）。",
            f"pg_restore 的原话：\n{_tail(result.stderr) or '（无输出）'}",
        )
    if "alembic_version" not in result.stdout:
        raise DrillError(
            EXIT_BACKUP,
            "归档里没有 alembic_version（不是一份已迁移的 PRDwright 库）。",
            "演练库要跑到迁移 head 之后再备份；检查第③步是否成功。",
        )
    table_data = sum(1 for line in result.stdout.splitlines() if "TABLE DATA public" in line)
    if table_data != expected_tables:
        raise DrillError(
            EXIT_BACKUP,
            f"归档里的表数与源库不一致：归档 {table_data} 张，源库 {expected_tables} 张。",
            "备份期间结构被改过，或演练库没跑完迁移。重跑一次演练。",
        )
    return table_data


def _destroy(kwargs: dict[str, object]) -> None:
    """砸掉整个 public schema：模拟「库坏了 / 误执行了 drop」的最坏情况。"""
    conn = _connect(kwargs, DRILL_DATABASE)
    try:
        with conn.cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE")
            cursor.execute("CREATE SCHEMA public")
    finally:
        conn.close()


def _restore(archive: Path, pg_image: str, kwargs: dict[str, object]) -> None:
    command = (
        [
            "docker",
            "run",
            "--rm",
            "--add-host=host.docker.internal:host-gateway",
            "-e",
            f"PGPASSWORD={kwargs['password']}",
            "-v",
            f"{archive.parent}:/backup:ro",
            pg_image,
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "-h",
            _container_host(str(kwargs["host"])),
            "-p",
            str(kwargs["port"]),
            "-U",
            str(kwargs["user"]),
            "-d",
            DRILL_DATABASE,
            f"{CONTAINER_BACKUP_DIR}/{archive.name}",
        ]
    )
    result = _run(command)
    if result.returncode != 0:
        raise DrillError(
            EXIT_RESTORE,
            "`pg_restore --clean --if-exists` 失败。",
            f"pg_restore 的原话：\n{_tail(result.stderr) or '（无输出）'}",
        )


def _verify_restored(kwargs: dict[str, object], baseline: dict[str, object]) -> dict[str, object]:
    conn = _connect(kwargs, DRILL_DATABASE)
    try:
        actual = _live_state(conn)
    finally:
        conn.close()

    if actual["tables"] != baseline["tables"]:
        raise DrillError(
            EXIT_VERIFY,
            f"表数对不上：备份前 {baseline['tables']} 张，恢复后 {actual['tables']} 张。",
            "恢复不完整。检查 ④ 的 pg_restore 输出里有没有 ERROR 行。",
        )
    if actual["revision"] != baseline["revision"]:
        raise DrillError(
            EXIT_VERIFY,
            f"alembic_version 对不上：备份前 {baseline['revision']}，恢复后 {actual['revision']}。",
            "恢复的是别处的归档，或 alembic_version 没被恢复。",
        )
    if actual["rows"] != baseline["rows"]:
        raise DrillError(
            EXIT_VERIFY,
            f"标记行对不上：备份前 {baseline['rows']}，恢复后 {actual['rows']}。",
            "restore 恢复出的是另一份数据。核对 ③④ 的库名。",
        )
    return actual


def _cleanup(maint_kwargs: dict[str, object], temp_dir: Path) -> None:
    """删演练库 + 删临时目录；不碰仓库里的任何文件。"""
    from psycopg import sql

    with _connect(maint_kwargs, MAINTENANCE_DATABASE) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(DRILL_DATABASE)
            )
        )
    shutil.rmtree(temp_dir, ignore_errors=True)


def _backup_dir_listing() -> set[str]:
    if not BACKUP_DIR.is_dir():
        return set()
    return {entry.name for entry in BACKUP_DIR.iterdir()}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "PRDwright 回滚预演练：在本机 Postgres 上真实跑一遍「备份 → 破坏 → 恢复」，"
            "记录每步耗时。只动演练库 prdwright_drill，产物不落 deploy/backups/。"
        ),
        epilog=(
            "示例：cd deploy && make drill\n"
            "      uv run --project ../backend python drill.py --keep"
        ),
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("PRDWRIGHT_DRILL_DSN", DEFAULT_DSN),
        help=f"维护库地址（不带库名），默认 {DEFAULT_DSN}。",
    )
    parser.add_argument(
        "--pg-image",
        default=f"postgres:{DEFAULT_PG_TAG}",
        help=f"跑 pg_dump / pg_restore 的客户端镜像，默认 postgres:{DEFAULT_PG_TAG}（与服务端同主版本）。",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="演练结束不清理（保留演练库与备份文件，仅用于排查）。",
    )
    return parser


def _main(args: argparse.Namespace) -> int:
    clock = StepClock()
    started = time.perf_counter()
    backups_before = _backup_dir_listing()
    temp_dir = Path(tempfile.mkdtemp(prefix="prdwright-drill-"))
    archive = temp_dir / ARCHIVE_NAME
    url = None
    keep = bool(args.keep)
    cleaned = False

    try:
        # ① 预检
        timer = _Timer()
        url = _parse_dsn(args.dsn)
        if str(temp_dir.resolve()).startswith(str(REPO_ROOT.resolve())):
            raise DrillError(EXIT_PRECHECK, "临时目录落在仓库里。", "系统临时目录不可写？检查 TMPDIR。")
        _precheck(args.pg_image)
        maint_kwargs = _psycopg_kwargs(url)
        # ⚠️ 必须 render_as_string(hide_password=False)：URL 的 __str__ 会把口令渲染成
        # `***`，拿它去连库会得到一个「password authentication failed」——看起来像库的
        # 问题，其实是自己把口令擦掉了。
        drill_url = url.set(database=DRILL_DATABASE).render_as_string(hide_password=False)
        drill_kwargs = {**maint_kwargs, "dbname": DRILL_DATABASE}
        clock.record("① 预检（docker / 镜像 / 本地库）", timer.elapsed)

        # ② 重建演练库
        timer = _Timer()
        _reset_database(maint_kwargs)
        clock.record("② 重建演练库 prdwright_drill", timer.elapsed)

        # ③ 迁移到 head
        timer = _Timer()
        revision = _migrate(drill_url)
        clock.record("③ 跑迁移到 head（应用同一个入口）", timer.elapsed)

        # ④ 造数据并记录水印
        timer = _Timer()
        seed_rows = _seed(drill_kwargs)
        with _connect(drill_kwargs, DRILL_DATABASE) as conn:
            baseline = _live_state(conn)
        if seed_rows != len(SEED_ROWS):
            raise DrillError(
                EXIT_BACKUP,
                f"造数据失败：期望 {len(SEED_ROWS)} 行，实际 {seed_rows} 行。",
                "看 ②③ 是否成功；system_config 的写入不该失败。",
            )
        clock.record("④ 造数据（system_config 3 行）", timer.elapsed)

        # ⑤ 备份 + 自检
        timer = _Timer()
        _dump(archive, args.pg_image, maint_kwargs, DRILL_DATABASE)
        digest = _sha256_file(archive)
        size = archive.stat().st_size
        table_data = _verify_archive(archive, args.pg_image, int(baseline["tables"]))
        clock.record("⑤ 备份（pg_dump -Fc + --list 自检）", timer.elapsed)

        # ⑥ 破坏
        timer = _Timer()
        _destroy(drill_kwargs)
        clock.record("⑥ 破坏（DROP SCHEMA public CASCADE）", timer.elapsed)

        # ⑦ 恢复
        timer = _Timer()
        _restore(archive, args.pg_image, maint_kwargs)
        clock.record("⑦ 恢复（pg_restore --clean --if-exists）", timer.elapsed)

        # ⑧ 校验
        timer = _Timer()
        restored = _verify_restored(drill_kwargs, baseline)
        clock.record("⑧ 校验（表数 / 版本 / 标记行）", timer.elapsed)

        # ⑨ 清理
        if keep:
            clock.record("⑨ 清理（--keep：保留现场）", 0.0)
        else:
            timer = _Timer()
            _cleanup(maint_kwargs, temp_dir)
            cleaned = True
            clock.record("⑨ 清理（删演练库 + 临时目录）", timer.elapsed)

        if _backup_dir_listing() != backups_before:
            raise DrillError(
                EXIT_VERIFY,
                "deploy/backups/ 的文件清单被改动了。",
                "演练不该往那里写东西；检查是否有别的进程同时在备份。",
            )

        # 总结
        total = time.perf_counter() - started
        print()
        print("[演练] ================ 演练通过 ================")
        print(f"[演练] 库               {DRILL_DATABASE}（每次重建，跑完{'保留' if keep else '删除'}）")
        print(f"[演练] 迁移版本          {revision}")
        print(f"[演练] 表数              备份前 {baseline['tables']} → 恢复后 {restored['tables']}")
        print(f"[演练] 标记行            {len(SEED_ROWS)} 行（{', '.join(k for k, _ in SEED_ROWS)}）恢复一致")
        print(f"[演练] 归档              {size} 字节，sha256 {digest[:16]}…，{table_data} 张表的数据")
        print(f"[演练] 备份产物          {'保留在 ' + str(temp_dir) if keep else '已随临时目录删除（不落 deploy/backups/）'}")
        print(f"[演练] 总耗时            {_fmt_seconds(total)}（分步：")
        for name, seconds, _ok in clock.steps:
            print(f"[演练]   {name:<32} {_fmt_seconds(seconds)}")
        print("[演练] 结论：这份备份**真的**能恢复；生产演练仍需在切换窗口按 ROLLBACK.md 走一遍。")
        return EXIT_OK
    except DrillError as exc:
        total = time.perf_counter() - started
        print(
            f"\n[演练] ✗ 失败于「{STEP_NAMES.get(exc.code, '未知步骤')}」：{exc.message}",
            file=sys.stderr,
        )
        if exc.hint:
            print(f"[演练] 下一步：{exc.hint}", file=sys.stderr)
        print(f"[演练] 失败前已用 {_fmt_seconds(total)}；清理见下。", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("\n[演练] 已中断。", file=sys.stderr)
        return 130
    finally:
        # 失败路径也要收尾（否则会留下演练库或临时目录）；成功路径已在第⑨步收过。
        if url is None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        elif not cleaned and not keep:
            try:
                _cleanup(_psycopg_kwargs(url), temp_dir)
            except Exception as exc:  # 清理失败不该掩盖真正的错误
                print(f"[演练] 清理没做完：{exc}", file=sys.stderr)
                shutil.rmtree(temp_dir, ignore_errors=True)
                print(
                    f"[演练] 临时目录已删；若演练库曾创建成功，请手动执行 "
                    f"`DROP DATABASE prdwright_drill`（库连不上时只能人来做）。",
                    file=sys.stderr,
                )
        elif keep:
            print(
                f"[演练] 现场保留：备份 {temp_dir}，演练库 {DRILL_DATABASE}。"
                " 用完记得 `DROP DATABASE prdwright_drill` 并删掉这个目录。",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    return _main(_build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
