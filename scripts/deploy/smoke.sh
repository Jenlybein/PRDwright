#!/usr/bin/env bash
# ===========================================================================
# PRDwright v9 整栈冒烟：CI 与本机共用同一份
#
# 做的事，按顺序：
#   1. 备好 deploy/.env（CI 的检出里没有这个文件）
#   2. 构建 api 镜像（生成主密钥的脚本在这个镜像里）
#   3. 卷里没有主密钥就生成一把（有则复用——本机重复跑不会撞上「拒绝覆盖」）
#   4. `docker compose up -d --build`
#   5. 轮询 `/readyz`，直到**响应体里的 `status == "ready"`**（有硬超时）
#   6. 断言主链路可达：`/` 与 `/admin/` 是 HTML、`/api/...` 经反代可达、`/healthz` 200，
#      并确认 api 的 **PID 1 以 uid 10001 运行**（#14 的「非 root」是可验证的）
#   7. 无论成败都拆栈（`down`，**不带 `-v`**）；失败时先输出容器日志
#
# 用法（在仓库任意目录都行，脚本自己切到仓库根）：
#   PRDWRIGHT_HTTP_PORT=8080 PRDWRIGHT_IMAGE_TAG="$(git describe --tags --always)" \
#     bash scripts/deploy/smoke.sh
#
# 环境变量：
#   PRDWRIGHT_HTTP_PORT   宿主端口，默认 8080（CI 避开特权端口 80）
#   PRDWRIGHT_IMAGE_TAG   镜像 tag，默认 local（与 compose 的默认值一致）
#   SMOKE_READY_TIMEOUT   就绪轮询的硬上限（秒），默认 180
#
# 边界（#10 / #19）：只验「容器能起来、主链路可达、运行身份正确」，**不跑登录与生成**——
# 那需要真实模型密钥，会把 CI 变成对外部服务的依赖。
#
# **明确没做的断言：SSE 不被缓冲。** 现有流式端点全都挂在登录 + 真实模型调用之后
# （`/api/conversation/*-stream`、`/api/jobs/{id}/stream`），
# 而冒烟不登录、也不调真实模型；`/readyz` 是普通 JSON，拿它测不出「首字节之后还有没有
# 分片陆续到达」。宁可不写，也不写一条看着像验证、其实证明不了缓冲行为的假断言：
# 缓冲行为目前由 `deploy/docker/nginx.conf` 的 `proxy_buffering off` 与 v7 §6 的
# SSE 三行契约保证，真机验证需要一次带模型的端到端手测。
#
# 为什么单独成脚本而不内联进 ci.yml：
#   1. 它是「轮询 + 多条断言 + 失败转储日志 + 收尾」四段有顺序的逻辑，内联会让
#      workflow 难以阅读；
#   2. 本机与 CI 跑的是**同一份代码**。否则「我在本机跑过等价命令」不能当作
#      「CI 上真的通过」的证据。
# ===========================================================================
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILE="deploy/compose.yaml"
HTTP_PORT="${PRDWRIGHT_HTTP_PORT:-8080}"
IMAGE_TAG="${PRDWRIGHT_IMAGE_TAG:-local}"
READY_TIMEOUT="${SMOKE_READY_TIMEOUT:-180}"
BASE_URL="http://127.0.0.1:${HTTP_PORT}"
SITE_DIR="$(dirname -- "$COMPOSE_FILE")"
ENV_FILE="${SITE_DIR}/.env"

# compose 的变量插值优先读进程环境，所以这里的 export 会覆盖 deploy/.env 里的同名值：
# .env 是「部署形态的样例」，CI 的端口与 tag 由 workflow 决定。
export PRDWRIGHT_HTTP_PORT="$HTTP_PORT"
export PRDWRIGHT_IMAGE_TAG="$IMAGE_TAG"

COMPOSE=(docker compose -f "$COMPOSE_FILE")

TMP_DIR="$(mktemp -d)"

fail() {
    printf '✗ %s\n' "$*" >&2
    exit 1
}

# 收尾：失败先转储容器日志（排查靠它），然后拆栈。
# `down` 不带 `-v`：卷（数据 / 日志 / 主密钥）必须留下——这是 compose 的契约，
# 也是「冒烟不会顺手删掉一份真实数据」的保证。
cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "===== 冒烟失败：容器状态 ====="
        "${COMPOSE[@]}" ps -a || true
        echo "===== 冒烟失败：容器日志（末尾 500 行）====="
        "${COMPOSE[@]}" logs --no-color --tail=500 || true
    fi
    rm -rf "$TMP_DIR" || true
    echo "===== 拆栈：docker compose down（保留卷，不带 -v）====="
    "${COMPOSE[@]}" down --remove-orphans || true
    exit "$status"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 前置：docker / compose / 一个能解析 JSON 的 python
# ---------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || fail "找不到 docker（本机需要能跑 Linux 容器）"
docker compose version >/dev/null 2>&1 || fail "docker compose v2 不可用"
command -v curl >/dev/null 2>&1 || fail "找不到 curl（断言要用它）"

# JSON 断言不用 grep 硬凑：`status == "ready"` 必须从解析后的 JSON 字段里取，
# 否则「HTTP 200 但返回的是 SPA 的 index.html」这种假绿会被放过（#16 的教训）。
if command -v python3 >/dev/null 2>&1 && python3 -c 'import json' >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1 && python -c 'import json' >/dev/null 2>&1; then
    PYTHON=python
else
    fail "找不到可用的 python3/python（就绪与 JSON 断言要用它解析响应体）"
fi

echo "===== 1/6 准备 ${ENV_FILE} ====="
if [ ! -f "$ENV_FILE" ]; then
    cp "${SITE_DIR}/.env.example" "$ENV_FILE"
    echo "已从 .env.example 生成 ${ENV_FILE}（CI 的检出里没有 .env）"
else
    echo "复用已有的 ${ENV_FILE}"
fi

# ---------------------------------------------------------------------------
# 2) 构建 api 镜像：主密钥生成器在镜像里，没有镜像就无法准备密钥
# ---------------------------------------------------------------------------
echo "===== 2/6 构建 api 镜像 ====="
build_started=$(date +%s)
"${COMPOSE[@]}" build api
echo "api 镜像构建完成，耗时 $(( $(date +%s) - build_started ))s"

# ---------------------------------------------------------------------------
# 3) 主密钥：**缺失时拒绝生成是应用的底线**，所以 CI 必须显式准备一把
#
# 不能用 `compose run api` 生成：compose 把密钥卷**只读**挂进 api（应用永远不该改
# 主密钥），那样写会得到 `Read-only file system`。这里照 README 的做法，用一次性
# 容器以**可写**方式挂同一个卷。
# 卷名与镜像名从 `compose config` 的解析结果里取，不在这里重抄常量：compose 的
# 项目名一旦被改（或本机用 COMPOSE_PROJECT_NAME 隔离测试），这里会跟着走。
# `--user 10001:10001` 是硬要求：用 root 生成会得到一个 600 且属主 root 的文件，
# 应用（uid 10001）读不到，症状与「密钥不存在」几乎一样。
# ---------------------------------------------------------------------------
echo "===== 3/6 准备主密钥（卷里已有则复用）====="
"${COMPOSE[@]}" config --format json > "$TMP_DIR/compose.json"
PROJECT_NAME="$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["name"])' < "$TMP_DIR/compose.json")"
API_IMAGE="$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["services"]["api"]["image"])' < "$TMP_DIR/compose.json")"
SECRETS_VOLUME="${PROJECT_NAME}_secrets"
echo "密钥卷 ${SECRETS_VOLUME}；镜像 ${API_IMAGE}"
docker run --rm --user 10001:10001 \
    -e PRDWRIGHT_MASTER_KEY_PATH=/secrets/master.key \
    -v "${SECRETS_VOLUME}:/secrets" \
    --entrypoint sh "${API_IMAGE}" -c '
        if [ -f "$PRDWRIGHT_MASTER_KEY_PATH" ]; then
            echo "[smoke] 主密钥已存在，跳过生成"
        else
            python scripts/keygen/generate_keys.py
        fi
    '

# ---------------------------------------------------------------------------
# 4) 起整栈。`--build` 让 web 也从当前源码现构建；api 这层已由第 2 步构建，
#    这里命中同一份本地层缓存，不会重装依赖。
# ---------------------------------------------------------------------------
echo "===== 4/6 起整栈（宿主端口 ${HTTP_PORT}，镜像 tag ${IMAGE_TAG}）====="
up_started=$(date +%s)
"${COMPOSE[@]}" up -d --build
up_seconds=$(( $(date +%s) - up_started ))
echo "up 命令完成，耗时 ${up_seconds}s（含镜像构建）"

# ---------------------------------------------------------------------------
# 5) 等就绪：判据是 JSON 里的 status，不是 HTTP 状态码
# ---------------------------------------------------------------------------
echo "===== 5/6 等待 /readyz 的 status 变为 ready（上限 ${READY_TIMEOUT}s）====="
deadline=$(( $(date +%s) + READY_TIMEOUT ))
attempt=0
ready=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    attempt=$((attempt + 1))
    code="$(curl -sS --max-time 5 -o "$TMP_DIR/readyz.json" -w '%{http_code}' "$BASE_URL/readyz" 2>/dev/null || true)"
    if [ "$code" = "200" ] && "$PYTHON" -c '
import json, sys
sys.exit(0 if json.load(sys.stdin).get("status") == "ready" else 1)
' < "$TMP_DIR/readyz.json" 2>/dev/null; then
        ready=1
        break
    fi
    if [ $((attempt % 10)) -eq 0 ]; then
        printf '  ... %ss：第 %s 次仍未就绪，HTTP %s，body=%s\n' \
            "$(( $(date +%s) - up_started ))" "$attempt" "${code:-无响应}" \
            "$(tr -d '\r\n' < "$TMP_DIR/readyz.json" 2>/dev/null | cut -c1-200)"
    fi
    sleep 2
done
if [ "$ready" -ne 1 ]; then
    fail "/readyz 在 ${READY_TIMEOUT}s 内没有返回 status == \"ready\"（最后一次：$(cat "$TMP_DIR/readyz.json" 2>/dev/null || echo 无响应)）"
fi
ready_seconds=$(( $(date +%s) - up_started ))
echo "✓ 就绪：up → /readyz ready 共 ${ready_seconds}s（其中 up 返回后等了 $((ready_seconds - up_seconds))s）"

# ---------------------------------------------------------------------------
# 6) 主链路断言
#
# 每条都带「期望内容」而不只是状态码：
#   · 首页 / 管理端：200 + text/html + 产物标记（防止 SPA 兜底把 404 也变成 200）
#   · /api/：免鉴权的问卷配置接口，200 + application/json（证明反代真的到了 api）
#   · /healthz：200 + {"ok": true}
# ---------------------------------------------------------------------------
echo "===== 6/6 断言主链路可达 ====="

HTTP_BODY_FILE=""
HTTP_CODE=""
HTTP_TYPE=""

fetch() {
    local path="$1"
    local hdr="$TMP_DIR/response.hdr"
    HTTP_BODY_FILE="$TMP_DIR/response.body"
    HTTP_CODE="$(curl -sS --max-time 10 -o "$HTTP_BODY_FILE" -D "$hdr" -w '%{http_code}' "$BASE_URL$path")" \
        || fail "GET ${path} 失败（服务没起来，或宿主端口 ${HTTP_PORT} 不通）"
    # 没有 content-type 时 grep 会返回 1，这里的 `|| true` 让 set -e 不把它当失败
    HTTP_TYPE="$( { grep -i '^content-type:' "$hdr" | tail -n 1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r'; } || true )"
}

assert_html() {
    local path="$1"
    local marker="$2"
    local label="$3"
    fetch "$path"
    [ "$HTTP_CODE" = "200" ] || fail "${path}：期望 HTTP 200，实际 ${HTTP_CODE}"
    case "$HTTP_TYPE" in
        text/html*) ;;
        *) fail "${path}：content-type 期望 text/html*，实际 '${HTTP_TYPE}'" ;;
    esac
    grep -qF 'id="app"' "$HTTP_BODY_FILE" || fail "${path}：响应体不是 SPA 的 index.html（缺 id=\"app\"）"
    grep -qF "$marker" "$HTTP_BODY_FILE" || fail "${path}：响应体缺少期望标记 ${marker}（拿到的可能是另一个前端的 index.html）"
    echo "✓ ${path} → 200 text/html（${label}）"
}

# 6.1 就绪探针要再取一次响应头：确认成功时的 content-type 与状态码也对得上
fetch /readyz
[ "$HTTP_CODE" = "200" ] || fail "/readyz：期望 HTTP 200，实际 ${HTTP_CODE}"
case "$HTTP_TYPE" in
    application/json*) ;;
    *) fail "/readyz：content-type 期望 application/json，实际 '${HTTP_TYPE}'" ;;
esac
"$PYTHON" -c '
import json, sys
sys.exit(0 if json.load(sys.stdin).get("status") == "ready" else 1)
' < "$HTTP_BODY_FILE" || fail "/readyz：status 不是 \"ready\""
echo "✓ /readyz → 200 application/json，status=ready"

# 6.2 生死探针：恒 200 且 ok=true（它不查依赖，所以这里只验「进程活着」这一件事）
fetch /healthz
[ "$HTTP_CODE" = "200" ] || fail "/healthz：期望 HTTP 200，实际 ${HTTP_CODE}"
case "$HTTP_TYPE" in
    application/json*) ;;
    *) fail "/healthz：content-type 期望 application/json，实际 '${HTTP_TYPE}'" ;;
esac
"$PYTHON" -c '
import json, sys
sys.exit(0 if json.load(sys.stdin).get("ok") is True else 1)
' < "$HTTP_BODY_FILE" || fail "/healthz：响应体不是 {\"ok\": true}"
echo "✓ /healthz → 200 application/json，ok=true"

# 6.3 用户端：产物里引用的是站点根的资源路径 `"/assets/..."`
assert_html / '"/assets/' "用户端 SPA"

# 6.4 管理端：产物挂在 `/admin/` 下，资源路径带 `/admin/` 前缀。
# 这条负向价值最大：nginx 兜底若错落成用户端 index.html，状态码同样是 200。
assert_html /admin/ '"/admin/assets/' "管理端 SPA，资源前缀 /admin/"

# 6.5 反代：问卷配置是公开白名单里的接口，未登录也应 200。
# 用 JSON 响应而不是「页面 200」来证明请求真的穿过了 `/api/` 反代到了 api。
fetch /api/conversation/questions
[ "$HTTP_CODE" = "200" ] || fail "/api/conversation/questions：期望 HTTP 200，实际 ${HTTP_CODE}"
case "$HTTP_TYPE" in
    application/json*) ;;
    *) fail "/api/conversation/questions：content-type 期望 application/json，实际 '${HTTP_TYPE}'" ;;
esac
"$PYTHON" -c '
import json, sys
doc = json.load(sys.stdin)
base = doc.get("base_questions")
advanced = doc.get("advanced_questions")
sys.exit(0 if isinstance(base, list) and base and isinstance(advanced, list) else 1)
' < "$HTTP_BODY_FILE" || fail "/api/conversation/questions：响应体不是问卷配置（base_questions 为空或不是数组）"
echo "✓ /api/conversation/questions → 200 application/json（/api/ 反代可达）"

# 6.6 运行身份：api 的**应用进程**必须是 uid 10001（#14 验收里「非 root」的自动化形式）
#
# 为什么不是 `docker compose exec api id -u`：镜像刻意没有 `USER` 指令（启动脚本第一阶段
# 必须是 root 才能校正卷属主，之后才 `exec gosu` 降权），而 `exec` 用的是**镜像的 User**，
# 所以那样读到的恒是 0。要证明的是「真正在跑业务的那个进程是谁」，判据是 PID 1 的 Uid
# ——启动脚本降权后 `gosu` 是 exec 而不是 fork，PID 1 就是应用本身。
API_UID="$(
    "${COMPOSE[@]}" exec -T api sh -c "sed -n 's/^Uid:[[:space:]]*//p' /proc/1/status" \
        | cut -f1 | tr -d '\r\n'
)"
[ "$API_UID" = "10001" ] || fail "api 的 PID 1 期望以 uid 10001 运行，实际「${API_UID:-读不到}」"
echo "✓ api 的 PID 1 以 uid 10001 运行（非 root）"

echo "===== 冒烟通过：整栈可起、主链路可达（up → ready ${ready_seconds}s）====="
