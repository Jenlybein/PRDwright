#!/usr/bin/env python3
"""PRDwright v9 备份脚本：`pg_dump` 产出一份带 sha256 的**可验证**备份。

用法（仓库根）::

    uv run --project backend python deploy/backup.py

用法（deploy/ 目录，与 README 其余命令一致）::

    uv run --project ../backend python deploy/backup.py

产物::

    deploy/backups/prdwright-YYYYmmdd-HHMM.dump
    deploy/backups/prdwright-YYYYmmdd-HHMM.dump.sha256

## 为什么是 pg_dump，而不是拷文件

v9 起数据库是 compose 里独立的 Postgres 服务（`db`），数据在 `prdwright_pgdata`
命名卷里。**没有**一个「拷一份目录」的正确做法：Postgres 的数据目录包含 WAL 与共享
内存状态，直接拷目录会得到一份「能启动、不报错、就是少了最近写入」的假备份。
`pg_dump -Fc` 走数据库的一致性快照，是唯一被官方支持的在线备份手段；栈起着也能备份。

## 为什么在宿主机跑、却把 pg_dump 放进容器

v9 的数据在 Docker 命名卷 / 独立服务里，宿主机上通常没有 `pg_dump`，也不该假设版本
一致。所以脚本本体跑在宿主的项目 uv 环境里（只用 stdlib），而真正的 dump 交给一次性
`postgres:<主版本>` 容器：

- 走 compose 的**用户自定义网络**（`prdwright_net`），服务名 `db` 由 Docker 内嵌 DNS 解析；
- 产物目录 bind mount 成 `/backup`，容器把 dump 直接写进宿主；
- **栈起着或停着都能备份**：`pg_dump` 是数据库侧的一致性快照，不依赖 api 容器活着。

## 自检写进退出码

备份完不算完，要能证明它不是空壳：

1. `pg_restore --list` 能解析这份 dump（归档完整、可读）；
2. 归档里的 `TABLE DATA` 条目数 == 源库 `public` 下的表数（没少表）；
3. 归档里有 `alembic_version`（这是一份已迁移的 PRDwright 库，不是空库）。

任一项失败即以非零状态退出，并删掉这份不可用的产物。

## 单一后端

本项目只支持 Postgres。`deploy/.env` 里若显式写了 `DATABASE_URL` 且指向别的后端
（SQLite / MySQL），前置检查会明确拒绝并以非零状态退出——不猜、不降级，也不存在
第二条备份流程。

## 退出码

=====  ==================================================================
0      成功
1      未归类失败（例如容器内出现未预期的异常）
2      前置检查失败：Docker / 镜像 / 网络 / 卷 / 主密钥 / 后端判定 / 连接参数
3      `pg_dump` 失败
4      自检失败（归档解析不了、表数对不上、读不到 `alembic_version`）
5      写盘或校验失败（宿主上的 sha256 / 字节数与预期不一致）
=====  ==================================================================
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy"
DEFAULT_OUTPUT_DIR = DEPLOY_DIR / "backups"

# 与 deploy/compose.yaml 的 `name: prdwright` 对齐：卷名与网络名因此固定，与目录名无关。
PROJECT_NAME = "prdwright"
DEFAULT_NETWORK = f"{PROJECT_NAME}_net"
SECRETS_VOLUME = f"{PROJECT_NAME}_secrets"

# compose 里数据库服务的服务名（容器网络上按这个名字解析）。
DEFAULT_DB_HOST = "db"
DEFAULT_DB_PORT = 5432

# 与 compose 的 `image: postgres:${PRDWRIGHT_PG_TAG:-16}` 对齐：pg_dump 的版本必须
# 不低于服务端主版本，否则 pg_dump 会直接拒绝。
DEFAULT_PG_TAG = "16"

# api 镜像只用来校验主密钥（它带着后端的 crypto 代码）。镜像 ENV 里的契约见
# deploy/docker/api.Dockerfile。
DEFAULT_MASTER_KEY_PATH = "/secrets/master.key"
CONTAINER_BACKUP_DIR = "/backup"

# 一次性容器的 pg_dump 是「数据库侧快照」，900 秒是「库/网络出问题时不无限挂着」的天花板。
DOCKER_BACKUP_TIMEOUT = 900

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PRECHECK = 2
EXIT_DUMP = 3
EXIT_SELFCHECK = 4
EXIT_VERIFY = 5

STEP_NAMES = {
    EXIT_FAIL: "未归类的失败",
    EXIT_PRECHECK: "前置检查",
    EXIT_DUMP: "数据库导出（pg_dump）",
    EXIT_SELFCHECK: "备份自检（pg_restore --list）",
    EXIT_VERIFY: "写盘与校验",
}


class BackupError(Exception):
    """一步失败：带退出码、人话说明、以及**能直接照做**的下一步提示。"""

    def __init__(self, code: int, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """跑一条宿主命令；Docker 没装时给一致的报错，而不是抛 traceback。"""
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
        raise BackupError(
            EXIT_PRECHECK,
            f"找不到命令：{cmd[0]}",
            "备份要在有 Docker 的机器上跑。装好 Docker Engine / Docker Desktop 后重试。",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(
            EXIT_FAIL,
            f"命令超时：{' '.join(cmd)}（>{timeout}s）",
            "Docker 可能卡住了；看一眼 `docker ps` 与 Docker Desktop 的状态再重试。",
        ) from exc


def _tail(text: str, limit: int = 1200) -> str:
    """只留尾部若干字符：错误信息要短，且原文里最有用的是最后几行。"""
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _env_file_value(name: str) -> str | None:
    """从 deploy/.env 读一个变量（compose 的 env_file，就是 api 容器里那套值的来源）。

    只做最小的解析：跳过注释与空行，去掉可选的 `export ` 与成对引号。值里不要写行尾
    注释（compose 的规则也一样），所以这里不把 `#` 当注释切。
    """
    path = DEPLOY_DIR / ".env"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep or key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return None


def _backup_name(output_dir: Path) -> str:
    """`prdwright-YYYYmmdd-HHMM.dump`；同一分钟内第二次备份退化为带秒。

    用宿主本地时间：运维按文件名找备份时，看的是自己的钟。
    """
    now = datetime.now()
    candidate = f"prdwright-{now:%Y%m%d-%H%M}.dump"
    if (output_dir / candidate).exists() or (
        output_dir / f"{candidate}.sha256"
    ).exists():
        candidate = f"prdwright-{now:%Y%m%d-%H%M%S}.dump"
    return candidate


# ---------------------------------------------------------------------------
# 连接参数：从 .env / compose 的同一套值里推导
# ---------------------------------------------------------------------------


class Connection:
    """备份要连的服务端参数（口令单独留在环境变量里，不进命令行）。"""

    def __init__(
        self, *, host: str, port: int, user: str, password: str, database: str
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database

    def env_flags(self) -> list[str]:
        # `docker run -e` 只传名字时，值取自宿主的同名环境变量；这里显式传
        # `NAME=value`，避免「宿主的 shell 里恰好有一个同名变量」把口令换掉。
        return [
            "-e",
            f"PGPASSWORD={self.password}",
            "-e",
            f"PGHOST={self.host}",
            "-e",
            f"PGPORT={self.port}",
            "-e",
            f"PGUSER={self.user}",
            "-e",
            f"PGDATABASE={self.database}",
        ]

    def describe(self) -> str:
        return (
            f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.database}"
        )


def _connection_from_env_file() -> Connection:
    """备份连的是**compose 里那个库**：先看 `.env` 是否显式写了 DATABASE_URL，否则按
    compose 拼 DATABASE_URL 的同一套规则推导（库名 / 用户 / 口令）。

    这等于「与 api 容器读同一份配置」：两边任何一处改了，这里立刻就对上。
    """
    explicit = (_env_file_value("DATABASE_URL") or "").strip()
    if explicit:
        parsed = urlparse(explicit)
        scheme = (parsed.scheme or "").split("+", 1)[0].lower()
        if scheme != "postgresql":
            raise BackupError(
                EXIT_PRECHECK,
                f"deploy/.env 的 DATABASE_URL 指向了 {scheme or '未知后端'}：本项目只支持 Postgres。",
                "本项目自 v9 起只支持 Postgres 单一后端（SQLite / MySQL 的备份流程已删除）。\n"
                "把 DATABASE_URL 改成 postgresql+psycopg://<用户>:<口令>@<主机>:5432/<库名>，"
                "或直接删掉这一行——默认会按 compose 的 POSTGRES_* 推导。",
            )
        if not parsed.hostname or not parsed.path.strip("/"):
            raise BackupError(
                EXIT_PRECHECK,
                f"DATABASE_URL 缺少主机或库名，无法备份：{explicit}",
                "补成 postgresql+psycopg://<用户>:<口令>@<主机>:5432/<库名>。",
            )
        password = unquote(parsed.password or "")
        if not password:
            raise BackupError(
                EXIT_PRECHECK,
                "deploy/.env 的 DATABASE_URL 里没有口令。",
                "备份要能在一次性容器里连上数据库；没有口令就没法连。\n"
                "把 DATABASE_URL 写全（或删掉它，让脚本按 POSTGRES_* 推导）。",
            )
        return Connection(
            host=parsed.hostname,
            port=parsed.port or DEFAULT_DB_PORT,
            user=unquote(parsed.username or ""),
            password=password,
            database=parsed.path.strip("/"),
        )

    user = (_env_file_value("POSTGRES_USER") or "prdwright").strip()
    password = (_env_file_value("POSTGRES_PASSWORD") or "").strip()
    database = (_env_file_value("POSTGRES_DB") or "prdwright").strip()
    if not password:
        raise BackupError(
            EXIT_PRECHECK,
            "deploy/.env 里没有 POSTGRES_PASSWORD，无法推导备份连接参数。",
            "compose 用这三项拼出 api 的 DATABASE_URL：POSTGRES_USER / POSTGRES_PASSWORD /\n"
            "POSTGRES_DB。按 deploy/.env.example 把口令补上（与正在运行的栈保持一致），"
            "或显式写一条 DATABASE_URL。",
        )
    return Connection(
        host=DEFAULT_DB_HOST,
        port=DEFAULT_DB_PORT,
        user=user,
        password=password,
        database=database,
    )


# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------


def _resolve_image(args: argparse.Namespace) -> str:
    """校验主密钥用的 api 镜像。"""
    if args.image:
        return args.image
    tag = os.environ.get("PRDWRIGHT_IMAGE_TAG") or _env_file_value("PRDWRIGHT_IMAGE_TAG")
    return f"prdwright-api:{tag or 'local'}"


def _resolve_pg_image(args: argparse.Namespace) -> str:
    """`pg_dump` 用的客户端镜像（与服务端同一主版本）。"""
    if args.pg_image:
        return args.pg_image
    tag = os.environ.get("PRDWRIGHT_PG_TAG") or _env_file_value("PRDWRIGHT_PG_TAG")
    return f"postgres:{tag or DEFAULT_PG_TAG}"


def _require_docker() -> None:
    result = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    if result.returncode != 0:
        raise BackupError(
            EXIT_PRECHECK,
            "Docker 守护进程不可用（`docker version` 连不上 server）。",
            "启动 Docker Desktop / `sudo systemctl start docker` 后重试。\n"
            f"docker 的原话：{_tail(result.stderr) or '（无输出）'}",
        )


def _require_image(image: str, hint: str) -> None:
    if _run(["docker", "image", "inspect", image], timeout=60).returncode != 0:
        raise BackupError(EXIT_PRECHECK, f"找不到镜像：{image}", hint)


def _require_network(name: str) -> None:
    if _run(["docker", "network", "inspect", name], timeout=60).returncode != 0:
        raise BackupError(
            EXIT_PRECHECK,
            f"找不到 Docker 网络：{name}",
            "这套栈还没起过（compose 会创建 prdwright_net）。\n"
            "先 `cd deploy && docker compose up -d`，等 db 健康、/readyz 200 再备份。",
        )


def _require_volume(name: str, purpose: str, hint: str) -> None:
    if _run(["docker", "volume", "inspect", name], timeout=60).returncode != 0:
        raise BackupError(EXIT_PRECHECK, f"{purpose}卷不存在：{name}", hint)


def _check_master_key(image: str) -> None:
    """主密钥缺失 / 不合法就拒绝备份。

    复用后端自己的 `encrypt_value`，而不是在这里另写一份「什么算合法 Fernet 密钥」：
    启动脚本（api-entrypoint.sh）也是这么做的，两处口径才不会分叉。没有密钥的备份
    解不开 `model_providers.value_token` 里的密文，等于无用备份。
    """
    command = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "python",
        "-e",
        f"PRDWRIGHT_MASTER_KEY_PATH={DEFAULT_MASTER_KEY_PATH}",
        "-v",
        f"{SECRETS_VOLUME}:/secrets:ro",
        image,
        "-c",
        "from backend.core.crypto import encrypt_value; encrypt_value('backup-probe')",
    ]
    result = _run(command, timeout=120)
    if result.returncode == 0:
        return
    raise BackupError(
        EXIT_PRECHECK,
        "主密钥不可用，拒绝备份（没有密钥的备份解不开库里的密文，等于无用备份）。",
        "按 deploy/README.md §1.2 生成主密钥（`make keygen` 或 docker run 那行命令）；"
        "若这个卷原本就有密钥（迁移/恢复），把旧文件放回并确保属主 10001、权限 600。\n"
        f"校验脚本的原话：\n{_tail(result.stderr) or '（无输出）'}",
    )


# ---------------------------------------------------------------------------
# 数据库状态与归档自检
# ---------------------------------------------------------------------------


def _docker_base(pg_image: str, network: str) -> list[str]:
    return ["docker", "run", "--rm", "--network", network]


def _live_state(connection: Connection, pg_image: str, network: str) -> tuple[str, int]:
    """从源库读「版本号」与「public 下的表数」——备份自检的独立参照。

    - `alembic_version`：证明源库是已迁移的 PRDwright 库；
    - 表数（含 `alembic_version`）：用来与归档里的 `TABLE DATA` 条目数对比，
      少表就是备份不完整（这种情况静默落盘最危险）。
    """
    sql_version = "SELECT version_num FROM alembic_version"
    sql_tables = (
        "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
    )
    command = (
        ["docker", "run", "--rm", "--network", network]
        + connection.env_flags()
        + [pg_image, "psql", "-tA", "-c", sql_version, "-c", sql_tables]
    )
    result = _run(command, timeout=120)
    if result.returncode != 0:
        raise BackupError(
            EXIT_SELFCHECK,
            "读不到源库的版本与水印（psql 失败）。",
            "数据库可能没起来，或凭据不对。先 `docker compose ps db` 与 "
            "`docker compose logs db`，再核对 .env 的 POSTGRES_* / DATABASE_URL。\n"
            f"psql 的原话：\n{_tail(result.stderr) or '（无输出）'}",
        )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise BackupError(
            EXIT_SELFCHECK,
            f"psql 的输出看不懂（期望两行：版本号与表数）：{lines}",
            "确认这个库是已迁移的 PRDwright 库（api 首次启动会跑迁移）。",
        )
    version = lines[-2]
    try:
        table_count = int(lines[-1])
    except ValueError as exc:
        raise BackupError(
            EXIT_SELFCHECK,
            f"psql 返回的表数不是整数：{lines[-1]!r}",
            "这多半说明连到了别的库。核对 DATABASE_URL / POSTGRES_DB。",
        ) from exc
    if not version:
        raise BackupError(
            EXIT_SELFCHECK,
            "源库的 alembic_version 是空的（没有迁移版本号）。",
            "库可能没跑完迁移。先起栈，等 /readyz 200 再备份。",
        )
    return version, table_count


def _verify_archive(archive: Path, pg_image: str, expected_tables: int) -> int:
    """用 `pg_restore --list` 解析归档，返回归档里的 `TABLE DATA` 条目数。

    不连库：`pg_restore --list` 只读归档本身，所以「源库在 dump 之后又被改过」不会
    被这条检查误判——这类变化由「表数对比」在 dump 当时抓。
    """
    command = (
        ["docker", "run", "--rm", "-v", f"{archive.parent}:/backup:ro"]
        + [pg_image, "pg_restore", "--list", f"{CONTAINER_BACKUP_DIR}/{archive.name}"]
    )
    result = _run(command, timeout=180)
    if result.returncode != 0:
        raise BackupError(
            EXIT_SELFCHECK,
            "`pg_restore --list` 解析不了这份归档（dump 不完整）。",
            "检查宿主机磁盘空间与输出目录权限；重跑一次备份。\n"
            f"pg_restore 的原话：\n{_tail(result.stderr) or '（无输出）'}",
        )
    toc = result.stdout
    if "alembic_version" not in toc:
        raise BackupError(
            EXIT_SELFCHECK,
            "归档里没有 alembic_version（它不是一个已迁移的 PRDwright 库）。",
            "确认 api 已经成功启动过（首次启动会跑迁移），再重跑备份。",
        )
    table_data = sum(1 for line in toc.splitlines() if "TABLE DATA public" in line)
    if table_data != expected_tables:
        raise BackupError(
            EXIT_SELFCHECK,
            f"归档里的表数与源库不一致：归档 {table_data} 张，源库 {expected_tables} 张。",
            "这不像一次「部分失败」，更像是 dump 期间结构被改了。等部署稳定后重跑；"
            "迁移进行中不要备份。",
        )
    return table_data


# ---------------------------------------------------------------------------
# 主流程（宿主机）
# ---------------------------------------------------------------------------


def _do_on_host(args: argparse.Namespace) -> None:
    connection = _connection_from_env_file()
    api_image = _resolve_image(args)
    pg_image = _resolve_pg_image(args)
    network = args.network or os.environ.get("PRDWRIGHT_DOCKER_NETWORK") or DEFAULT_NETWORK
    output_dir = Path(args.output_dir).expanduser().resolve()

    _require_docker()
    _require_image(
        api_image,
        "先用 compose 构建 api 镜像（只需一次）：\n"
        "  cd deploy && docker compose build api\n"
        "或直接起栈（会顺带构建）：cd deploy && docker compose up -d --build",
    )
    _require_image(
        pg_image,
        f"备份用与服务端同主版本的 pg_dump。先拉一次：docker pull {pg_image}\n"
        "（compose 起 db 时也会拉同一个 tag，见 compose.yaml 的 image 行）。",
    )
    _require_network(network)
    _require_volume(
        f"{PROJECT_NAME}_pgdata",
        "数据库数据",
        "数据卷不存在说明这套栈还没起过（或卷被 `docker compose down -v` 删了）。\n"
        "先起栈（cd deploy && docker compose up -d），等 db 健康、/readyz 200 再备份。\n"
        "如果这是新机器，本机没有可备份的数据——别把它当成「备份失败」。",
    )
    _require_volume(
        SECRETS_VOLUME,
        "主密钥",
        "主密钥卷不存在，说明主密钥从未生成过。\n"
        "先按 deploy/README.md §1.2 生成主密钥再备份。\n"
        "**没有密钥的备份解不开密文，等于无用备份，所以脚本拒绝继续。**",
    )
    _check_master_key(api_image)

    output_dir.mkdir(parents=True, exist_ok=True)
    name = _backup_name(output_dir)
    destination = output_dir / name
    container_name = f"prdwright-backup-{datetime.now():%Y%m%d%H%M%S}"

    version, table_count = _live_state(connection, pg_image, network)

    print(
        f"[备份] Postgres：{connection.describe()}（网络 {network}）\n"
        f"[备份] 镜像 {pg_image}；产物目录 {output_dir}\n"
        f"[备份] 源库版本 {version}；public 表 {table_count} 张\n"
        f"[备份] 在一次性容器里执行 pg_dump…"
    )

    # 产物直接写进宿主目录（bind mount）。先写成 `.partial`，自检通过再改名：
    # 半截文件绝不能以「正式备份」的名字留在目录里。
    partial = destination.with_name(destination.name + ".partial")
    dump_command = (
        _docker_base(pg_image, network)
        + ["--name", container_name]
        + connection.env_flags()
        + ["-v", f"{output_dir}:/backup:rw"]
        + [
            pg_image,
            "pg_dump",
            "-Fc",
            "--no-owner",
            "--no-privileges",
            "-f",
            f"{CONTAINER_BACKUP_DIR}/{partial.name}",
        ]
    )
    try:
        result = _run(dump_command, timeout=DOCKER_BACKUP_TIMEOUT)
    except BackupError:
        _force_remove_container(container_name)
        partial.unlink(missing_ok=True)
        raise
    if result.returncode != 0:
        _force_remove_container(container_name)
        partial.unlink(missing_ok=True)
        raise BackupError(
            EXIT_DUMP,
            f"pg_dump 失败（退出码 {result.returncode}）。",
            "常见原因：数据库不可达（网络 / 凭据）、dump 期间拿不到锁、磁盘空间不足。\n"
            "先看 `docker compose ps db` 与 `docker compose logs db`，再重试。\n"
            f"pg_dump 的原话：\n{_tail(result.stderr) or '（无输出）'}",
        )
    if result.stderr.strip():
        # 成功路径上的 stderr 只应是提示；原样转达，不藏。
        print(result.stderr.strip(), file=sys.stderr)
    if not partial.is_file() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        raise BackupError(
            EXIT_SELFCHECK,
            "pg_dump 报告成功，但宿主上看不到产物（或它是空文件）。",
            "检查 deploy/backups 是否被别的进程清掉、或 Docker Desktop 的文件共享是否正常。",
        )

    # 自检：解析归档 → 通过后进入正式名字（此后任何一个失败都要删掉它）。
    os.replace(partial, destination)
    _verify_archive(destination, pg_image, table_count)

    try:
        host_sha = _sha256_file(destination)
        host_bytes = destination.stat().st_size
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise BackupError(
            EXIT_VERIFY,
            f"读不了刚写出的备份：{destination}（{exc}）",
            "检查输出目录权限与磁盘空间后重跑。",
        ) from exc

    sha_path = output_dir / f"{name}.sha256"
    try:
        tmp_sha = sha_path.with_name(sha_path.name + ".tmp")
        tmp_sha.write_text(f"{host_sha}  {name}\n", encoding="utf-8")
        os.replace(tmp_sha, sha_path)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise BackupError(
            EXIT_VERIFY,
            f"写 sha256 文件失败：{sha_path}（{exc}）",
            "备份文件已经写好了，但少了校验和就不算可验证；检查目录权限后重跑。",
        ) from exc

    _restore_host_ownership(destination, args.owner)

    print(
        "\n[备份] ✓ 备份完成\n"
        f"        文件         {destination}\n"
        f"        字节数       {host_bytes}\n"
        f"        sha256       {host_sha}\n"
        f"        自检结论     ok（归档可解析、表数 {table_count} 与源库一致、"
        f"alembic_version={version}）\n"
        f"        校验和文件   {sha_path}\n"
        f"\n[备份] 校验：cd {output_dir} && sha256sum -c {sha_path.name}\n"
        f"[备份] Windows 用 Get-FileHash -Algorithm SHA256 与 .sha256 文件首列比对。\n"
        f"[备份] 恢复步骤见 deploy/README.md §6.4；主密钥要单独保管，它不在这个包里。"
    )


def _force_remove_container(name: str) -> None:
    """删掉可能残留的一次性容器（`--rm` 正常退出时它已经没了，这里是兜底）。"""
    try:
        _run(["docker", "rm", "-f", name], timeout=60)
    except BackupError:
        pass


def _restore_host_ownership(destination: Path, owner: str | None) -> None:
    """容器以 root 跑（要读卷）；产物落回宿主后改回宿主用户，免得 Linux 上留下
    root 拥有的备份文件。Windows 没有这套语义，跳过；失败只提示不改结果。
    """
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    if owner:
        try:
            uid_text, _, gid_text = owner.partition(":")
            os.chown(destination, int(uid_text), int(gid_text))
        except (ValueError, OSError) as exc:
            print(
                f"[备份] 提示：无法把备份属主改成 {owner}（{exc}）；文件内容不受影响。",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _default_owner() -> str | None:
    """Linux/macOS 上把产物改回调用者；Windows 返回 None（没有这套语义）。"""
    if not hasattr(os, "getuid"):
        return None
    return f"{os.getuid()}:{os.getgid()}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "PRDwright v9 备份：本项目只支持 Postgres。走 pg_dump -Fc + "
            "pg_restore --list 自检，产物落 deploy/backups/ 并带同名 .sha256。"
            "DATABASE_URL 指向 SQLite / MySQL 时一律明确拒绝。"
        ),
        epilog=(
            "示例：uv run --project backend python deploy/backup.py\n"
            "      uv run --project backend python deploy/backup.py --output-dir /mnt/backup"
        ),
    )
    parser.add_argument(
        "--image",
        help="校验主密钥用的应用镜像；默认按 PRDWRIGHT_IMAGE_TAG 推导 prdwright-api:<tag>。",
    )
    parser.add_argument(
        "--pg-image",
        help=f"pg_dump 用的客户端镜像；默认 postgres:{DEFAULT_PG_TAG}（与服务端同主版本）。",
    )
    parser.add_argument(
        "--network",
        help=f"一次性容器接入的 Docker 网络；默认 {DEFAULT_NETWORK}。",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="宿主上的产物目录；默认 deploy/backups/。",
    )
    parser.add_argument(
        "--owner",
        default=_default_owner(),
        help="产物属主（uid:gid）；默认当前用户，Windows 上忽略。",
    )
    return parser


def _main(args: argparse.Namespace) -> int:
    try:
        _do_on_host(args)
    except BackupError as exc:
        print(
            f"[备份] ✗ 失败于「{STEP_NAMES.get(exc.code, '未知步骤')}」：{exc.message}",
            file=sys.stderr,
        )
        if exc.hint:
            print(f"[备份] 下一步：{exc.hint}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("\n[备份] 已中断；未完成的产物已删除。", file=sys.stderr)
        return 130
    return EXIT_OK


def _configure_stdio() -> None:
    """把输出流切到 UTF-8。

    Windows 上默认控制台编码是 GBK（cp936），而本脚本的提示文案里有 `✓` 这类字符——
    不切换的话，**成功路径的最后一行总结会以 `UnicodeEncodeError` 结束整个进程**，
    让一次已经成功的备份看起来像失败（产物其实已经写好了，错误发生在打印阶段）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # 非 TTY / 被重定向到不支持的文件对象
            continue


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = _build_parser().parse_args(argv)
    return _main(args)


if __name__ == "__main__":
    raise SystemExit(main())
