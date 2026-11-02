#!/usr/bin/env python3
"""PRDwright v9 备份脚本：按后端分派，产出带 sha256 的**可验证**备份。

用法（仓库根）::

    uv run --project backend python deploy/backup.py

用法（deploy/ 目录，与 README 其余命令一致）::

    uv run --project ../backend python backup.py

产物::

    deploy/backups/prdwright-YYYYmmdd-HHMM.db
    deploy/backups/prdwright-YYYYmmdd-HHMM.db.sha256

## 为什么在宿主机跑、却把库操作放进容器

v9 的数据在 Docker **命名卷**里，宿主机上没有一个「实时库路径」可指。所以脚本本体跑在
宿主的项目 uv 环境里（`backend/.venv`，只用 stdlib），而读写库那一步交给一次性的
`docker run --rm`，把 `prdwright_data` 卷挂进容器里做。这样：

- **栈起着也能备份**：`VACUUM INTO` 本来就是 SQLite 的在线备份，允许并发写入；
- **栈停着也能备份**：不依赖 api 容器活着，也不依赖容器的固定名字；
- **不需要 `docker cp`**：容器把自检通过的库文件直接写进宿主挂上来的
  `deploy/backups/`（一次写完的普通文件，不涉及 SQLite 的文件锁与共享内存映射，
  所以 Windows 上桥接目录的坑不会出现在这一步）；
- **SQLite 的所有文件语义都发生在命名卷/容器原生文件系统上**，不在宿主 bind mount 上，
  避开「Windows 宿主目录不支持 WAL 的 -shm」这个已知坑。

## 为什么 SQLite 用 `VACUUM INTO` 而不是 `cp`

库开了 WAL：最近写入可能还只在 `-wal` 边车文件里。直接拷 `.db` 会得到一个「能打开、
不报错、就是少了最近几分钟数据」的备份——这正是要消灭的假备份。`VACUUM INTO` 走
SQLite 自己的读事务，把当前可见的完整内容（含 WAL 里未 checkpoint 的帧）写成一个
独立文件，因此它才是「在线一致快照」。

## 自检写进退出码

备份完不算完，要能证明它不是空壳：能打开 + `PRAGMA integrity_check` = `ok` +
读得到 `alembic_version`（说明这是一份已迁移的 PRDwright 库，不是空文件）。
任一项失败即以非零状态退出，并且**不留下一个看起来可用的产物**。

## 后端分派

SQLite 是 v9 唯一在跑的后端。PostgreSQL 只留分派位：v9 的 `db` 服务是默认关闭的
profile，接入在 v10；这里**不臆造**未演练的 `pg_dump` 实现，而是明确报「未启用」并
非零退出。MySQL 在 v9 已不是一等后端，同样明确拒绝。

## 退出码

=====  ================================================================
0      成功
1      未归类失败（例如容器内出现未预期的异常）
2      前置检查失败：Docker / 镜像 / 卷 / 库文件 / 主密钥 / 后端分派
3      在线备份失败（`VACUUM INTO`）
4      自检失败（打不开、`integrity_check` 不为 ok、读不到 `alembic_version`）
5      写盘或校验失败（拷贝到宿主目录、宿主 sha256 与容器内不一致）
=====  ================================================================

`--in-container` 是内部模式，由宿主模式自动调用，不是给运维敲的。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import shutil
import sqlite3
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy"
DEFAULT_OUTPUT_DIR = DEPLOY_DIR / "backups"

# 与 deploy/compose.yaml 的 `name: prdwright` 对齐：卷名因此固定，与目录名无关。
DEFAULT_DATA_VOLUME = "prdwright_data"
DEFAULT_SECRETS_VOLUME = "prdwright_secrets"

# 镜像 ENV 里的契约（见 deploy/docker/api.Dockerfile）：库与主密钥在容器内的位置。
DEFAULT_DB_PATH = "/var/lib/prdwright/prdwright.db"
DEFAULT_MASTER_KEY_PATH = "/secrets/master.key"
CONTAINER_BACKUP_DIR = "/backup"
CONTAINER_SCRIPT_PATH = "/app/backup.py"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PRECHECK = 2
EXIT_VACUUM = 3
EXIT_SELFCHECK = 4
EXIT_VERIFY = 5

STEP_NAMES = {
    EXIT_FAIL: "未归类的失败",
    EXIT_PRECHECK: "前置检查",
    EXIT_VACUUM: "在线备份（VACUUM INTO）",
    EXIT_SELFCHECK: "备份自检",
    EXIT_VERIFY: "写盘与校验",
}

# 备份文件里用到的固定 SQL；集中在这里，方便对照 docs/agents/conventions.md §11。
PRAGMA_INTEGRITY = "PRAGMA integrity_check"
SQL_ALEMBIC_VERSION = "SELECT version_num FROM alembic_version"
SQL_TABLE_COUNT = (
    "SELECT COUNT(*) FROM sqlite_master "
    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
)


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


def _url_backend(url: str) -> str:
    """SQLAlchemy URL → 后端名（`sqlite+pysqlite` 这类取 `+` 前那段）。"""
    scheme = url.split("://", 1)[0].strip().lower().split("+", 1)[0]
    aliases = {
        "sqlite": "sqlite",
        "sqlite3": "sqlite",
        "postgres": "postgresql",
        "postgresql": "postgresql",
        "mysql": "mysql",
        "mariadb": "mysql",
    }
    return aliases.get(scheme, scheme or "unknown")


def _sqlite_absolute_path(url: str) -> str | None:
    """从 SQLite URL 取绝对路径；相对路径（`sqlite:///foo.db`）返回 None。

    SQLAlchemy 的形态：`sqlite:///relative.db` 与 `sqlite:////abs/path.db` 的区别是
    后者在 `sqlite://` 之后多一个 `/`。只有绝对路径能映射进命名卷。
    """
    rest = url.split("://", 1)[1]
    if rest.startswith("//"):
        return "/" + rest.lstrip("/")
    return None


def _backup_name(output_dir: Path) -> str:
    """`prdwright-YYYYmmdd-HHMM.db`；同一分钟内第二次备份退化为带秒。

    用宿主本地时间：运维按文件名找备份时，看的是自己的钟。
    """
    now = datetime.now()
    candidate = f"prdwright-{now:%Y%m%d-%H%M}.db"
    if (output_dir / candidate).exists() or (output_dir / f"{candidate}.sha256").exists():
        candidate = f"prdwright-{now:%Y%m%d-%H%M%S}.db"
    return candidate


def _parse_json_line(stdout: str) -> dict | None:
    """取容器输出里最后一行 JSON（成功或失败的信封）。"""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


# ---------------------------------------------------------------------------
# 宿主机模式
# ---------------------------------------------------------------------------


def _resolve_backend(args: argparse.Namespace) -> str:
    if args.backend:
        return args.backend
    # `.env` 是 compose 的 env_file，也就是 api 容器实际读到的值；宿主 shell 里的
    # DATABASE_URL 不会被 compose 自动注入，所以不拿它当判据（避免「以为切了库」）。
    url = _env_file_value("DATABASE_URL")
    if url:
        return _url_backend(url)
    return "sqlite"


def _resolve_db_path(args: argparse.Namespace, backend: str) -> str:
    if args.db_path:
        return args.db_path
    url = _env_file_value("DATABASE_URL")
    if url and backend == "sqlite":
        path = _sqlite_absolute_path(url)
        if path:
            return path
        raise BackupError(
            EXIT_PRECHECK,
            f"DATABASE_URL 里的 SQLite 路径不是绝对路径，无法映射进数据卷：{url}",
            "用 --db-path 指定容器内的绝对路径（v9 默认 "
            f"{DEFAULT_DB_PATH}）。",
        )
    return DEFAULT_DB_PATH


def _resolve_image(args: argparse.Namespace) -> str:
    if args.image:
        return args.image
    # compose 的插值优先级：宿主 shell 的变量 > .env；两边都没设时是 local。
    tag = os.environ.get("PRDWRIGHT_IMAGE_TAG") or _env_file_value("PRDWRIGHT_IMAGE_TAG")
    return f"prdwright-api:{tag or 'local'}"


def _require_docker() -> None:
    result = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    if result.returncode != 0:
        raise BackupError(
            EXIT_PRECHECK,
            "Docker 守护进程不可用（`docker version` 连不上 server）。",
            "启动 Docker Desktop / `sudo systemctl start docker` 后重试。\n"
            f"docker 的原话：{_tail(result.stderr) or '（无输出）'}",
        )


def _require_image(image: str) -> None:
    result = _run(["docker", "image", "inspect", image], timeout=60)
    if result.returncode != 0:
        raise BackupError(
            EXIT_PRECHECK,
            f"找不到镜像：{image}",
            "先用 compose 构建 api 镜像（只需一次）：\n"
            "  cd deploy && docker compose build api\n"
            "或直接起栈（会顺带构建）：cd deploy && docker compose up -d --build",
        )


def _require_volume(name: str, purpose: str, hint: str) -> None:
    if _run(["docker", "volume", "inspect", name], timeout=60).returncode != 0:
        raise BackupError(EXIT_PRECHECK, f"{purpose}卷不存在：{name}", hint)


def _force_remove_container(name: str) -> None:
    """删掉可能残留的一次性容器（`--rm` 正常退出时它已经没了，这里是兜底）。"""
    try:
        _run(["docker", "rm", "-f", name], timeout=60)
    except BackupError:
        pass


def _do_on_host(args: argparse.Namespace) -> None:
    backend = _resolve_backend(args)

    if backend == "postgresql":
        raise BackupError(
            EXIT_PRECHECK,
            "PostgreSQL 备份在 v9 未启用：compose 里的 db 服务是默认关闭的 profile，"
            "api 的库仍是 SQLite 文件库。",
            "这里只保留了分派位置，没有写未经演练的 pg_dump 流程；"
            "PostgreSQL 接入是 v10 的范围（接上之后按 docs/architecture/v9/ARCHITECTURE.md "
            "§6.2 的约定补 pg_dump -Fc 与 pg_restore --list 自检）。"
            "现在请备份 SQLite：去掉 .env 里的 DATABASE_URL，或加 --backend sqlite。",
        )
    if backend == "mysql":
        raise BackupError(
            EXIT_PRECHECK,
            "MySQL 不是 v9 的部署后端（v8 已把它从一等后端降级）。",
            "v9 只有 SQLite 在跑；换库见 v10。用 --backend sqlite 备份当前库。",
        )
    if backend != "sqlite":
        raise BackupError(
            EXIT_PRECHECK,
            f"不认识的数据库后端：{backend}",
            "v9 只支持 sqlite（PostgreSQL 见 v10）。检查 .env 里的 DATABASE_URL。",
        )

    db_path = _resolve_db_path(args, backend)
    image = _resolve_image(args)
    data_volume = args.data_volume or DEFAULT_DATA_VOLUME
    secrets_volume = args.secrets_volume or DEFAULT_SECRETS_VOLUME
    output_dir = Path(args.output_dir).expanduser().resolve()

    _require_docker()
    _require_image(image)
    _require_volume(
        data_volume,
        "数据",
        f"数据卷不存在说明这套栈还没起过（或卷被 `docker compose down -v` 删了）。\n"
        "先起栈（cd deploy && docker compose up -d --build），等 /readyz 返回 200 再备份。\n"
        "如果这是新机器，本机没有可备份的数据——别把它当成「备份失败」。",
    )
    _require_volume(
        secrets_volume,
        "主密钥",
        "主密钥卷不存在，说明主密钥从未生成过。\n"
        "先按 deploy/README.md §1.2 生成主密钥再备份。\n"
        "**没有密钥的备份解不开密文，等于无用备份，所以脚本拒绝继续。**",
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    name = _backup_name(output_dir)
    db_parent = posixpath.dirname(db_path) or posixpath.dirname(DEFAULT_DB_PATH)
    script_path = Path(__file__).resolve()
    container_name = f"prdwright-backup-{datetime.now():%Y%m%d%H%M%S}"

    print(
        f"[备份] 后端 sqlite；库 {db_path}（数据卷 {data_volume}）\n"
        f"[备份] 镜像 {image}；产物目录 {output_dir}\n"
        f"[备份] 在一次性容器里执行 VACUUM INTO 与自检…"
    )

    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--entrypoint",
        "python",
        "-v",
        f"{script_path}:{CONTAINER_SCRIPT_PATH}:ro",
        "-v",
        f"{data_volume}:{db_parent}",
        "-v",
        f"{secrets_volume}:/secrets:ro",
        "-v",
        f"{output_dir}:{CONTAINER_BACKUP_DIR}",
    ]
    env_file = DEPLOY_DIR / ".env"
    if env_file.is_file():
        # 与 api 容器读同一份 env_file：主密钥路径等覆盖项才和运行中的服务一致。
        command += ["--env-file", str(env_file)]
    command += [
        image,
        CONTAINER_SCRIPT_PATH,
        "--in-container",
        "--db",
        db_path,
        "--dest",
        f"{CONTAINER_BACKUP_DIR}/{name}",
        "--staging",
        f"/tmp/{name}.staging.db",
    ]
    if hasattr(os, "getuid"):
        # 容器以 root 跑（要读 10001 属主的卷）；产物落回宿主后改回宿主用户，
        # 免得 Linux 上留下一堆 root 拥有的备份文件。Windows 没有这套语义，跳过。
        command += ["--owner", f"{os.getuid()}:{os.getgid()}"]

    try:
        result = _run(command, timeout=args.timeout)
    except BackupError:
        _force_remove_container(container_name)
        raise
    if result.returncode != 0:
        _force_remove_container(container_name)
        envelope = _parse_json_line(result.stdout)
        if envelope and envelope.get("status") == "error":
            raise BackupError(
                int(envelope.get("code") or EXIT_FAIL),
                str(envelope.get("message") or "容器内备份失败"),
                str(envelope.get("hint") or ""),
            )
        raise BackupError(
            result.returncode if result.returncode in STEP_NAMES else EXIT_FAIL,
            f"容器内备份失败（退出码 {result.returncode}）。",
            f"docker 的原话：\n{_tail(result.stderr) or '（无输出）'}",
        )

    if result.stderr.strip():
        # 成功路径上的 stderr 只应是提示（例如无法改属主）；原样转达，不藏。
        print(result.stderr.strip(), file=sys.stderr)

    envelope = _parse_json_line(result.stdout)
    if not envelope or envelope.get("status") != "ok":
        raise BackupError(
            EXIT_VERIFY,
            "容器没有返回可解析的备份结果。",
            f"docker 的输出：\n{_tail(result.stdout) or '（空）'}",
        )

    destination = output_dir / name
    sha_path = output_dir / f"{name}.sha256"
    try:
        host_sha = _sha256_file(destination)
        host_bytes = destination.stat().st_size
    except OSError as exc:
        raise BackupError(
            EXIT_VERIFY,
            f"宿主上看不到备份产物：{destination}（{exc}）",
            "容器报告写成功了但宿主目录里没有：检查 deploy/backups 是否被别的进程清掉、"
            "或 Docker Desktop 的文件共享是否正常。",
        ) from exc

    container_sha = str(envelope.get("sha256", ""))
    if host_sha != container_sha or host_bytes != int(envelope.get("bytes", -1)):
        destination.unlink(missing_ok=True)
        raise BackupError(
            EXIT_VERIFY,
            "拷贝到宿主后的 sha256 / 字节数与容器内不一致，已删除这份可疑产物。",
            f"容器内 sha256={container_sha}，宿主上={host_sha}，"
            f"容器内字节数={envelope.get('bytes')}，宿主上={host_bytes}。\n"
            "多半是宿主目录的写入出了问题；换一个 --output-dir 重试，并检查磁盘。",
        )

    try:
        tmp_sha = sha_path.with_name(sha_path.name + ".tmp")
        tmp_sha.write_text(f"{host_sha}  {name}\n", encoding="utf-8")
        os.replace(tmp_sha, sha_path)
    except OSError as exc:
        raise BackupError(
            EXIT_VERIFY,
            f"写 sha256 文件失败：{sha_path}（{exc}）",
            "备份库文件已经写好了，但少了校验和就不算可验证；检查目录权限后重跑。",
        ) from exc

    print(
        "\n[备份] ✓ 备份完成\n"
        f"        文件         {destination}\n"
        f"        字节数       {host_bytes}\n"
        f"        sha256       {host_sha}\n"
        f"        自检结论     ok（库可打开、integrity_check 通过、"
        f"alembic_version={envelope.get('alembic_version')}、"
        f"表 {envelope.get('tables')} 张）\n"
        f"        校验和文件   {sha_path}\n"
        f"\n[备份] 校验：cd {output_dir} && sha256sum -c {sha_path.name}\n"
        f"[备份] Windows 用 Get-FileHash -Algorithm SHA256 与 .sha256 文件首列比对。\n"
        f"[备份] 恢复步骤见 deploy/README.md §6.4；主密钥要单独保管，它不在这个包里。"
    )


# ---------------------------------------------------------------------------
# 容器内模式（由宿主模式自动调用）
# ---------------------------------------------------------------------------


def _check_master_key(path: Path) -> None:
    """主密钥缺失 / 不合法就拒绝备份。

    复用后端自己的 `encrypt_value`，而不是在这里另写一份「什么算合法 Fernet 密钥」：
    启动脚本（api-entrypoint.sh）也是这么做的，两处口径才不会分叉。没有密钥的备份
    解不开 `model_providers.value_token` 里的密文，等于无用备份。
    """
    if not path.is_file():
        raise BackupError(
            EXIT_PRECHECK,
            f"找不到主密钥文件：{path}（拒绝备份）。",
            "没有密钥的备份解不开库里的密文，等于无用备份。\n"
            "按 deploy/README.md §1.2 生成主密钥（`make keygen` 或 docker run 那行命令）；"
            "若这个卷原本就有密钥（迁移/恢复），把旧文件放回并确保属主 10001、权限 600。",
        )
    try:
        from backend.core.crypto import encrypt_value
    except Exception as exc:  # noqa: BLE001 - 兜底成可读错误，不抛 traceback
        raise BackupError(
            EXIT_PRECHECK,
            f"无法加载后端的密钥校验逻辑（{type(exc).__name__}: {exc}）。",
            "这是脚本运行环境的问题（PYTHONPATH 不对？），不是密钥本身的问题；"
            "请用镜像里的 python 执行本脚本。",
        ) from exc
    try:
        encrypt_value("backup-probe")
    except RuntimeError as exc:
        raise BackupError(
            EXIT_PRECHECK,
            f"主密钥不可用：{path}（{exc}）",
            "文件在，但内容不是合法的 Fernet 密钥。别覆盖它——先从备份找回生成这份库时用的"
            "那把密钥；确认找不回再考虑重新生成（那会让库里已有的密文永久解不开）。",
        ) from exc
    except OSError as exc:
        raise BackupError(
            EXIT_PRECHECK,
            f"主密钥读不出来：{path}（{type(exc).__name__}: {exc}）",
            "多半是属主或权限不对（该文件应属运行用户 10001、权限 600）。"
            "用 --user 10001:10001 重新生成，或修正卷里的属主。",
        ) from exc


def _vacuum_into(db_path: Path, staging: Path) -> None:
    """在线备份：VACUUM INTO 到容器内的临时文件。

    临时文件放容器 `/tmp`（原生文件系统），不写命名卷、也不写宿主 bind mount；
    失败时只是丢一份临时文件，不会污染实时库。
    """
    print(f"[备份] VACUUM INTO → {staging}", file=sys.stderr)
    try:
        connection = sqlite3.connect(str(db_path), timeout=60)
        try:
            # 文件名用参数绑定：路径由脚本生成，但仍不拼字符串。
            connection.execute("VACUUM INTO ?", (str(staging),))
        finally:
            connection.close()
    except sqlite3.Error as exc:
        staging.unlink(missing_ok=True)
        raise BackupError(
            EXIT_VACUUM,
            f"VACUUM INTO 失败：{exc}",
            "常见原因：库文件不可读、磁盘/卷空间不足、或写锁一直拿不到（database is locked）。\n"
            "重试一次；仍失败就看 `docker compose logs api` 与卷的剩余空间。"
            "**不要退回去直接 cp 库文件**——WAL 会让那份备份静默少掉最近写入。",
        ) from exc


def _self_check(staging: Path) -> dict[str, object]:
    """打开备份并做三项检查：能打开、integrity_check = ok、alembic_version 可读。"""
    try:
        connection = sqlite3.connect(str(staging), timeout=30)
        try:
            integrity_rows = [
                str(row[0]) for row in connection.execute(PRAGMA_INTEGRITY).fetchall()
            ]
            if integrity_rows != ["ok"]:
                raise BackupError(
                    EXIT_SELFCHECK,
                    "PRAGMA integrity_check 未通过："
                    + "; ".join(integrity_rows[:5])
                    + ("（还有更多）" if len(integrity_rows) > 5 else ""),
                    "这份快照是坏的就不要用。先看运行中服务的 /readyz 与 /health，"
                    "确认源库状态，并考虑改用更早的备份。",
                )
            try:
                row = connection.execute(SQL_ALEMBIC_VERSION).fetchone()
            except sqlite3.Error as exc:
                raise BackupError(
                    EXIT_SELFCHECK,
                    f"备份里读不到 alembic_version 表：{exc}",
                    "这说明它不是一个已迁移的 PRDwright 库（空库或不是这个应用的库），"
                    "不能当作可用备份。确认 api 已经成功启动过（首次启动会跑迁移）。",
                ) from exc
            if row is None:
                raise BackupError(
                    EXIT_SELFCHECK,
                    "备份里的 alembic_version 表是空的（没有迁移版本号）。",
                    "库可能没跑完迁移。先起栈，等 /readyz 200 再备份。",
                )
            table_count = int(connection.execute(SQL_TABLE_COUNT).fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error as exc:
        staging.unlink(missing_ok=True)
        raise BackupError(
            EXIT_SELFCHECK,
            f"打不开备份文件：{exc}",
            "快照文件不完整（磁盘满？）。检查空间后重试。",
        ) from exc

    return {
        "status": "ok",
        "bytes": staging.stat().st_size,
        "sha256": _sha256_file(staging),
        "integrity": "ok",
        "alembic_version": str(row[0]),
        "tables": table_count,
    }


def _copy_to_host(staging: Path, destination: Path, owner: str | None) -> None:
    """把自检通过的快照放到宿主备份目录：先写 `.partial`，再原子改名。"""
    partial = destination.with_name(destination.name + ".partial")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staging, partial)
        with partial.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, destination)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise BackupError(
            EXIT_VERIFY,
            f"把备份写到宿主目录失败：{exc}",
            "检查 deploy/backups（或 --output-dir 指向的目录）的权限与磁盘空间。"
            "注意：这个文件在容器里看是 /backup，实际落在宿主上。",
        ) from exc

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


def _do_in_container(args: argparse.Namespace) -> dict[str, object]:
    if not args.db or not args.dest:
        raise BackupError(
            EXIT_PRECHECK,
            "内部参数缺失：--in-container 需要 --db 与 --dest（这是脚本自己该传的）。",
            "直接用宿主模式：uv run --project backend python deploy/backup.py",
        )

    db_path = Path(args.db)
    staging = Path(args.staging or f"/tmp/{Path(args.dest).name}.staging.db")
    destination = Path(args.dest)
    master_key = Path(
        args.master_key
        or os.environ.get("PRDWRIGHT_MASTER_KEY_PATH", DEFAULT_MASTER_KEY_PATH)
    )

    _check_master_key(master_key)

    if not db_path.is_file():
        raise BackupError(
            EXIT_PRECHECK,
            f"数据卷里找不到库文件：{db_path}",
            "api 可能从未成功启动过（库是首次启动时建的）。\n"
            "先起栈并确认 /readyz 返回 200，再备份。若路径被改过，用 --db-path 指对。",
        )

    staging.unlink(missing_ok=True)
    try:
        _vacuum_into(db_path, staging)
        payload = _self_check(staging)
        _copy_to_host(staging, destination, args.owner)
    finally:
        # 临时快照用完就删；它在容器的 /tmp 里，不会留到下一次。
        staging.unlink(missing_ok=True)
        for sidecar in (Path(f"{staging}-wal"), Path(f"{staging}-shm")):
            sidecar.unlink(missing_ok=True)
    return payload


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "PRDwright v9 备份：按后端分派。SQLite 走 VACUUM INTO + "
            "integrity_check + alembic_version 自检，产物落 deploy/backups/ 并带同名 "
            ".sha256。PostgreSQL 在 v9 未启用（只留分派位）。"
        ),
        epilog=(
            "示例：uv run --project backend python deploy/backup.py\n"
            "      uv run --project backend python deploy/backup.py --backend sqlite"
        ),
    )
    parser.add_argument(
        "--backend",
        choices=["sqlite", "postgresql", "mysql"],
        help="数据库后端；默认按 deploy/.env 的 DATABASE_URL 判断，没有则 sqlite。",
    )
    parser.add_argument(
        "--image",
        help="提供 python 的应用镜像；默认按 PRDWRIGHT_IMAGE_TAG 推导 prdwright-api:<tag>。",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="宿主上的产物目录；默认 deploy/backups/。",
    )
    parser.add_argument(
        "--data-volume",
        help=f"数据命名卷；默认 {DEFAULT_DATA_VOLUME}。",
    )
    parser.add_argument(
        "--secrets-volume",
        help=f"主密钥命名卷；默认 {DEFAULT_SECRETS_VOLUME}。",
    )
    parser.add_argument(
        "--db-path",
        help=f"容器内库文件路径；默认 {DEFAULT_DB_PATH}（数据卷挂到它的父目录）。",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="容器内备份的超时秒数；默认 900。",
    )

    # 内部参数：宿主模式会自己传，不暴露在 --help 里。
    internal = argparse.ArgumentParser(add_help=False)
    internal.add_argument("--in-container", action="store_true", help=argparse.SUPPRESS)
    internal.add_argument("--db", help=argparse.SUPPRESS)
    internal.add_argument("--dest", help=argparse.SUPPRESS)
    internal.add_argument("--staging", help=argparse.SUPPRESS)
    internal.add_argument("--master-key", help=argparse.SUPPRESS)
    internal.add_argument("--owner", help=argparse.SUPPRESS)
    for action in internal._actions:
        parser._add_action(action)
    return parser


def _main_on_host(args: argparse.Namespace) -> int:
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
        print("\n[备份] 已中断；未完成的临时文件由容器自身清理。", file=sys.stderr)
        return 130
    return EXIT_OK


def _main_in_container(args: argparse.Namespace) -> int:
    try:
        payload = _do_in_container(args)
    except BackupError as exc:
        # 用一行 JSON 把「错在哪一步、下一步做什么」交回宿主，由宿主统一排版。
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": exc.code,
                    "message": exc.message,
                    "hint": exc.hint,
                },
                ensure_ascii=False,
            )
        )
        return exc.code
    except Exception:  # noqa: BLE001 - 未预期异常要留完整 traceback，但退出码别是 0
        traceback.print_exc()
        return EXIT_FAIL
    print(json.dumps(payload, ensure_ascii=False))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return _main_in_container(args) if args.in_container else _main_on_host(args)


if __name__ == "__main__":
    raise SystemExit(main())
