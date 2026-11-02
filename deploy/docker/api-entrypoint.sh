#!/bin/sh
# ---------------------------------------------------------------------------
# PRDwright 应用镜像的启动脚本（ENTRYPOINT）
#
#   root 进入 → 校正卷属主 → 校验主密钥 → 降权 exec 应用
#
# 四步的顺序不能换，每一步都在回答一个「不做会怎样」：
#
# 1. **校正属主**（只在 root 下做）。命名卷首次创建时属主是 root，而应用以固定 uid
#    10001 运行。不校正的话症状不是「权限错误」，而是 SQLite 建库时的
#    `unable to open database file`、日志静默退回「只打 stdout」——两种都难归因，
#    而且都发生在「你以为服务起来了」之后。校正只发生在启动那一刻，之后进程一直非 root。
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

RUN_USER="${PRDWRIGHT_RUN_USER:-prdwright}"
KEY_FILE="${PRDWRIGHT_MASTER_KEY_PATH:-/secrets/master.key}"
PYTHON="${PRDWRIGHT_PYTHON:-/opt/venv/bin/python}"
IMAGE_REF="${PRDWRIGHT_IMAGE_REF:-prdwright-api:local}"
SECRETS_VOLUME="${PRDWRIGHT_SECRETS_VOLUME:-prdwright_secrets}"

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

    # 需要可写的目录：数据卷 + 日志卷。`DATABASE_URL` 指到别处时那个目录同样是挂载点，
    # 一并校正——否则「按文档改了库路径」就变成一次静默的「属主没改」。
    # 只解析 SQLite 的绝对路径形态（`sqlite:////abs/path`）；换成 MySQL/PG 之后
    # 数据目录不由容器管，这里自然不做任何事。
    dirs="${PRDWRIGHT_DATA_DIR:-/var/lib/prdwright} ${LOG_DIR:-/var/log/prdwright}"
    case "${DATABASE_URL:-}" in
        sqlite*:///*) dirs="$dirs $(dirname "${DATABASE_URL#sqlite*:///}")" ;;
    esac

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
check_master_key
guard_single_worker "$@"

if [ "$(id -u)" -eq 0 ]; then
    log "降权执行（uid=$(id -u "$RUN_USER" 2>/dev/null || echo '?')）：$*"
    exec gosu "$RUN_USER" "$@"
fi

log "执行（容器已以非 root 启动）：$*"
exec "$@"
