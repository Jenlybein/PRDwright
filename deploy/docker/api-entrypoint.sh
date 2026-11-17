#!/bin/sh
# ---------------------------------------------------------------------------
# PRDwright 应用镜像的启动脚本（ENTRYPOINT）
#
#   root 进入 → 校正卷属主 → 校验主密钥 → 降权 exec 应用
#
# 四步的顺序不能换，每一步都在回答一个「不做会怎样」：
#
# 1. **校正属主**（只在 root 下做）。命名卷首次创建时属主是 root，而应用以固定 uid
#    10001 运行。不校正的话症状不是「权限错误」，而是日志卷写不进去（静默退回「只打
#    stdout」）——难归因，而且发生在「你以为服务起来了」之后。校正只发生在启动那一刻，
#    之后进程一直非 root。数据库文件不在这个镜像的卷里（它是独立的 `db` 服务）。
#
# 2. **校验主密钥**。后端只在**第一次真正用到**密钥时才抛 RuntimeError
#    （`core/crypto.py::_fernet`），而 `/readyz` 只查「库可写 + 技能可加载」，
#    所以缺密钥的容器会正常起来、两条探针全绿，直到有人第一次配模型才炸。
#    把它提前到启动：缺了就退出，并打印一条能直接粘贴执行的生成命令。
#    **绝不自动生成**——自动生成会让换机后的旧密文永久解不开，而当场没有任何症状。
#
# 3. **降权**。`gosu` 是 exec 而不是 fork：降权后的进程替换掉脚本，于是 PID 1 就是应用
#    本身，SIGTERM 直达 uvicorn（#13 的排空靠这条）。
#
# 4. **执行命令**。单 worker 写死在镜像的 CMD 里，这里再拦一道（见文件末尾），
#    把「有人绕过 CMD 追加 --workers 4」这个误用空间也关掉。
#
# 迁移**不在这里跑**：只由应用的 `lifespan`（`prepare_schema`）跑一次。两个入口各跑一遍
# 的话，迁移日志与锁行为都不可预测（v9 的 C-V9-4）。
# ---------------------------------------------------------------------------
set -eu

RUN_USER="prdwright"
KEY_FILE="${PRDWRIGHT_MASTER_KEY_PATH:-/secrets/master.key}"
PYTHON="/opt/venv/bin/python"
IMAGE_REF="${PRDWRIGHT_IMAGE_REF:-prdwright-api:local}"
# 卷名由 compose 里的 `name: prdwright` 钉死（`prdwright_secrets`），不随目录改名。
SECRETS_VOLUME="prdwright_secrets"
#
# 上面几个里只有 KEY_FILE / IMAGE_REF / DATABASE_URL 读环境变量：它们分别由 compose 的 `.env` 与
# Dockerfile 的构建参数给值。`RUN_USER` / `PYTHON` / `SECRETS_VOLUME` 是写死的——它们与
# 镜像里的 `useradd`、venv 路径、compose 的项目名成对，没有任何编排者会设它们；
# 留一个可覆盖的口子只会让「镜像与编排不一致」成为可能。

log() { printf '[api-entrypoint] %s\n' "$*"; }

die() {
    printf '[api-entrypoint] ✗ %s\n' "$*" >&2
    exit 1
}

# 以运行用户身份执行一条命令：root 时经 gosu 降权，已经不是 root 时直接跑
# （`docker run --user 10001` 这种用法也要能用，只是跳过属主校正）。
run_as_user() {
    if [ "$(id -u)" -eq 0 ]; then
        gosu "$RUN_USER" "$@"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# 一、校正挂载点属主
# ---------------------------------------------------------------------------
prepare_dirs() {
    if [ "$(id -u)" -ne 0 ]; then
        log "以非 root 身份启动（uid=$(id -u)），跳过属主校正"
        return 0
    fi

    # 需要可写的目录：只剩日志卷。数据库文件不在这个容器里——v9 起它是 compose 里
    # 独立的 `db` 服务，data 目录由 Postgres 镜像自己管。
    dirs="${LOG_DIR:-/var/log/prdwright}"

    for dir in $dirs; do
        [ -n "$dir" ] || continue
        [ -d "$dir" ] || mkdir -p "$dir" || die "无法创建目录 $dir（卷没挂上？）"
        # 目录里已经全是运行用户时立刻跳过：正常重启不必每次都递归扫一遍日志卷。
        if [ -n "$(find "$dir" ! -user "$RUN_USER" -print -quit 2>/dev/null)" ]; then
            chown -R "$RUN_USER:$RUN_USER" "$dir" || die "无法把 $dir 的属主校正到 $RUN_USER"
            log "已把 $dir 的属主校正为 $RUN_USER"
        fi
    done
}

# ---------------------------------------------------------------------------
# 二、等数据库就绪
#
# compose 里 api 已经 `depends_on: db: service_healthy`，正常编排下这一步立刻就过。
# 但这条等待不是冗余的：
# - 直接 `docker run`（不经 compose）时没有 depends_on；
# - db 容器在一次网络抖动 / 重启里短暂不可达时，api 起得太早会因「连不上库」而退出，
#   然后由 restart 策略反复重启——每次都要重新等一遍，日志里只有连接错误。
#
# 判据是**真的能执行一条 SELECT**，不是「端口连得上」：Postgres 接受 TCP 之后还要
# 等它完成恢复流程才真正能接查询。
# ---------------------------------------------------------------------------
wait_for_database() {
    [ -n "${DATABASE_URL:-}" ] || die "没有配置 DATABASE_URL：本项目只支持 Postgres，且不提供默认库。
  请用 compose 起栈（它会按 .env 里的凭据拼出 DATABASE_URL），或显式传：
  -e DATABASE_URL=postgresql+psycopg://<用户>:<口令>@<主机>:5432/<库名>"

    case "${DATABASE_URL}" in
        postgres*://*) ;;
        *)
            die "DATABASE_URL 不是 Postgres 地址：${DATABASE_URL}
  本项目自 v9 起只支持 Postgres（SQLite / MySQL 的分支已删除，旧库也不会被自动升级）。
  请改成 postgresql+psycopg://<用户>:<口令>@<主机>:5432/<库名>。"
            ;;
    esac

    timeout="${DB_WAIT_TIMEOUT:-60}"
    log "等待数据库就绪（最多 ${timeout}s）：$(printf '%s' "$DATABASE_URL" | sed -E 's#(://[^:/@]*):[^@]*@#\1:***@#')"
    status=0
    # 用镜像里的 python + psycopg，不额外装 psql 客户端；连接串要换成 libpq 认的形态
    # （去掉 SQLAlchemy 的 `+psycopg` 驱动后缀）。
    run_as_user "$PYTHON" - "$timeout" <<'PY' || status=$?
import os
import sys
import time

import psycopg

raw = os.environ["DATABASE_URL"]
url = raw.replace("postgresql+psycopg://", "postgresql://", 1).replace(
    "postgresql+psycopg2://", "postgresql://", 1
)
deadline = time.time() + float(sys.argv[1])
last_error = None
while time.time() < deadline:
    try:
        with psycopg.connect(url, connect_timeout=3) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001 - 探针要兜住任何连接错误
        last_error = exc
        time.sleep(1)
sys.stderr.write(f"等待数据库超时（{sys.argv[1]}s）：{type(last_error).__name__}: {last_error}\n")
sys.exit(1)
PY

    [ "$status" -eq 0 ] || die "数据库在 ${timeout}s 内没有就绪（见上一行的真实错误）。
  排查：docker compose ps db（容器是否 healthy）、docker compose logs db（initdb / 恢复是否失败）、
  以及 .env 里的 POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB 是否与已初始化的 pgdata 卷一致
  （改了凭据但卷没重建时，Postgres 仍用旧口令，见 deploy/README.md §6.5）。"
    log "数据库已就绪"
}

# ---------------------------------------------------------------------------
# 二、校验主密钥（缺失 / 读不到 / 内容非法 → 拒绝启动并给出可执行的下一步）
# ---------------------------------------------------------------------------
key_guidance() {
    printf '\n[api-entrypoint] ✗ %s\n' "$1" >&2
    cat >&2 <<EOF

主密钥决定库里所有模型密钥能不能解开。它缺失时**拒绝启动**，也**绝不自动生成**：
自动生成一把新密钥会让旧密文永久解不开，而且当场没有任何症状，只会在某天调模型时
表现为「密钥明明配了却报错」。

生成一把并写进密钥卷（只需一次，之后每次启动都复用它）：

  docker run --rm --user $(id -u "$RUN_USER" 2>/dev/null || echo 10001):$(id -g "$RUN_USER" 2>/dev/null || echo 10001) \\
    -e PRDWRIGHT_MASTER_KEY_PATH=${KEY_FILE} \\
    -v ${SECRETS_VOLUME}:/secrets \\
    --entrypoint python ${IMAGE_REF} \\
    /app/scripts/keygen/generate_keys.py

三点提醒：
  · 上面的卷名是默认推测；用 compose 起的栈里卷名带项目前缀，先跑
    docker compose config --volumes 看一眼，再把 -v 那行换掉。
  · 若这个卷里**原本就有**主密钥（迁移 / 恢复备份），不要重新生成：把旧文件放回去，
    并确保属主是 $(id -u "$RUN_USER" 2>/dev/null || echo 10001)、权限 600。
  · --user 与运行用户一致，是为了让生成出来的文件属于运行用户；
    用 root 生成会得到一个属主 root 的 600 文件，应用读不到，症状与「密钥不存在」几乎一样。

EOF
    exit 1
}

check_master_key() {
    # 密钥目录**不创建**：卷没挂上时要报错，而不是在一个空目录里等一把永远不会出现的密钥。
    [ -f "$KEY_FILE" ] || key_guidance "找不到主密钥文件：${KEY_FILE}"

    # 用**运行用户**的身份去校验：顺带验证「降权之后还读得到」这件真正会出事的事。
    # 校验逻辑复用后端自己的 `encrypt_value`，不在这里复制一份「什么算合法 Fernet 密钥」，
    # 于是它抛的那句中文 RuntimeError 就是这里要展示的原文。
    status=0
    run_as_user "$PYTHON" - <<'PY' || status=$?
import sys

from backend.core.crypto import encrypt_value

try:
    encrypt_value("startup-probe")
except RuntimeError as exc:  # 文件缺失 / 内容不是合法 Fernet 密钥
    sys.stderr.write(f"{exc}\n")
    sys.exit(2)
except OSError as exc:  # 存在但读不出来（属主或权限不对）
    sys.stderr.write(f"主密钥读不出来（{type(exc).__name__}: {exc}）\n")
    sys.exit(3)
PY

    case "$status" in
        0) ;;
        2) key_guidance "主密钥不可用：${KEY_FILE}" ;;
        3) key_guidance "主密钥存在但运行用户读不到：${KEY_FILE}（属主或权限不对）" ;;
        *) die "校验主密钥时出错（退出码 ${status}）" ;;
    esac
    log "主密钥可用：${KEY_FILE}"
}

# ---------------------------------------------------------------------------
# 三、单 worker 兜底：`--workers` 只允许 1
# ---------------------------------------------------------------------------
reject_multi_worker() {
    printf '[api-entrypoint] ✗ 拒绝启动：命令里出现了 --workers %s\n' "$1" >&2
    cat >&2 <<'EOF'

单 worker 是**正确性约束**，不是性能选择：任务运行时表、任务总线、配置快照、
固定连接池（max_overflow=0）、技能包缓存都是进程内单例。多一个 worker 就会出现
「A 进程起的任务，B 进程的 SSE 永远等不到分片」，表现为前端一直转圈而没有报错。
镜像的 CMD 里已经写死 `--workers 1`，不要在这里覆盖它。
EOF
    exit 1
}

guard_single_worker() {
    expect_workers=""
    for arg in "$@"; do
        if [ -n "$expect_workers" ]; then
            [ "$arg" = "1" ] || reject_multi_worker "$arg"
            expect_workers=""
            continue
        fi
        case "$arg" in
            --workers) expect_workers=1 ;;
            --workers=*)
                [ "${arg#--workers=}" = "1" ] || reject_multi_worker "${arg#--workers=}"
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
[ "$#" -gt 0 ] || die "没有要执行的命令（ENTRYPOINT 需要 CMD，例如 uvicorn backend.main:app）"

prepare_dirs
wait_for_database
check_master_key
guard_single_worker "$@"

if [ "$(id -u)" -eq 0 ]; then
    log "降权执行（uid=$(id -u "$RUN_USER" 2>/dev/null || echo '?')）：$*"
    exec gosu "$RUN_USER" "$@"
fi

log "执行（容器已以非 root 启动）：$*"
exec "$@"
