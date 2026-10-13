"""错误契约的不变量。

改造前，前端靠 `message.includes('会话不存在')` 这类**中文文案匹配**来分支，
后端改一个字判断就静默失效——没有报错、只有行为悄悄退化。本次改造把判据换成
机器可读的 `code`（见 `core/errors.py` 与前端 `services/errors.ts`）。

本文件锁的是这套契约的**不变量**，而不是某句具体文案：

1. 每一个错误响应都必须带 `code`，且取值在 `ErrorCode` 之内（含中间件路径的 401）；
2. 前后端两张码表必须**完全相等**——只改一边就红，这是防漂移的核心守卫；
3. 前端源码里不得再出现「对含中文的字符串做判断」；
4. 接口层不得再出现裸 `HTTPException`（它会造出没有 `code` 的响应）。

其中 3 与 4 是**静态扫描**：它们守的是「以后别再退回去」，跑一次 pytest 就能发现。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import login, register

from backend.core.errors import ErrorCode

# 仓库根 → 前端源码目录 / 后端接口层目录（跨端与跨层守卫用）
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_SRC = _REPO_ROOT / "frontend" / "src"
_BACKEND_API_SRC = _REPO_ROOT / "backend" / "src" / "backend" / "api"

# 后端承诺的全部错误码取值
_KNOWN_CODES = {code.value for code in ErrorCode}

# 一份最小可用的方案快照，`data` 字段只要求非空
_SOLUTION_PAYLOAD = {"title": "越权用例的方案", "mode": "structured", "stage": "form", "data": "{}"}


# ============================================================================
# 不变量 1：每个错误响应都带 code
# ============================================================================


def _middleware_401(client):
    """未登录访问受保护接口：由认证中间件直接拦下（还没进路由）。"""
    return client.get("/api/solutions")


def _auth_me_401(client):
    """未登录查询登录态：由路由层抛出（`/api/auth` 在中间件白名单里）。"""
    return client.get("/api/auth/me")


def _register_400(client):
    """注册失败：邀请码无效。"""
    return client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password123", "invite_code": "wrong-invite"},
    )


def _login_401(client):
    """登录失败：密码错误。"""
    register(client)
    client.post("/api/auth/logout")
    return login(client, password="wrong-password")


def _foreign_solution_404(client):
    """方案越权：读别人的方案，对外等同于不存在。"""
    register(client, username="alice")
    created = client.post("/api/solutions", json=_SOLUTION_PAYLOAD)
    client.post("/api/auth/logout")
    register(client, username="bob")
    return client.get(f"/api/solutions/{created.json()['id']}")


def _empty_input_400(client):
    """请求语义不合法：summary 与 requirements 都没给。"""
    register(client)
    return client.post("/api/conversation/generate-prd-from-summary-stream", json={})


def _validation_422(client):
    """Pydantic 字段校验失败：用户名短于 3 个字符。"""
    return client.post(
        "/api/auth/register",
        json={"username": "ab", "password": "password123", "invite_code": "test-invite"},
    )


@pytest.mark.parametrize(
    ("case", "send"),
    [
        ("中间件 401", _middleware_401),
        ("auth/me 401", _auth_me_401),
        ("注册失败 400", _register_400),
        ("登录失败 401", _login_401),
        ("方案越权 404", _foreign_solution_404),
        ("参数全空 400", _empty_input_400),
        ("字段校验 422", _validation_422),
    ],
)
def test_every_error_response_carries_code(client, case, send):
    """所有错误响应都必须带 `code`，且 `detail` 保持原有的非空中文。

    `detail` 只断言「非空」而不比对措辞：措辞是给人看的，由各自的业务测试去锁，
    这里要锁的是「机器读的那一份始终在场」。
    """
    response = send(client)
    body = response.json()

    assert response.status_code >= 400, f"{case}：期望是错误响应，实际 {response.status_code}"
    assert "code" in body, f"{case}：响应体缺少 code 字段 → {body}"
    assert body["code"] in _KNOWN_CODES, f"{case}：code 取值不在契约内 → {body['code']}"
    assert body.get("detail"), f"{case}：detail 不能为空 → {body}"


def test_validation_error_keeps_list_detail(client):
    """422 的 `detail` 仍是 FastAPI 原样的列表，只是并列补上了 `code`。

    前端解析 422 依赖这个结构（逐条取字段级错误），退化成字符串会静默丢信息。
    """
    response = client.post(
        "/api/auth/register",
        json={"username": "ab", "password": "password123", "invite_code": "test-invite"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == ErrorCode.VALIDATION_ERROR.value
    assert isinstance(body["detail"], list) and body["detail"]
    assert "loc" in body["detail"][0]


def test_expired_conversation_stream_carries_code(client):
    """会话失效时，SSE 流里的 `error` 事件必须带 `code`。

    这条路径是本次改造的**起因**：错误发生在 HTTP 200 的流内，前端拿不到状态码，
    只能靠事件里的 `code` 判断要不要降级重试。因此这里直接读流文本，
    断言顶层真有 `code` 字段——而不是只测 `StreamEvent` 这个数据类（那测不到
    「流有没有真的把它序列化出去」）。
    """
    register(client)
    response = client.post(
        "/api/conversation/continue-stream",
        json={"conversation_id": "ghost-conversation", "content": "继续聊"},
    )

    assert response.status_code == 200, "会话失效返回的是 HTTP 200 的流，错不体现在状态码上"

    events = [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:") and line.strip() != "data: [DONE]"
    ]
    error_events = [event for event in events if event.get("type") == "error"]

    assert len(error_events) == 1, f"期望恰好一个 error 事件，实际 {events}"
    assert error_events[0]["code"] == ErrorCode.SESSION_NOT_FOUND.value
    # 文案照旧给人和日志看，但它不是判据——这里不比对它的内容
    assert error_events[0]["content"]


def test_middleware_and_route_401_share_the_same_shape(client):
    """中间件拦下的 401 与路由抛出的 401 必须是同一种东西。

    前者走 ASGI 原生消息、后者走异常处理器，两条路稍不一致，前端就得分成两码事。
    """
    middleware = client.get("/api/solutions").json()
    route = client.get("/api/auth/me").json()

    assert set(middleware) == set(route) == {"detail", "code"}
    assert middleware["code"] == route["code"] == ErrorCode.UNAUTHORIZED.value


# ============================================================================
# 不变量 2：前后端码表严格一致
# ============================================================================

# 匹配 `export const BACKEND_ERROR_CODES = { ... } as const` 的常量表主体
_CODE_TABLE_RE = re.compile(
    r"BACKEND_ERROR_CODES[^=]*=\s*\{(?P<body>.*?)\}\s*as\s+const",
    re.DOTALL,
)
# 匹配表内条目：`SESSION_NOT_FOUND: 'SESSION_NOT_FOUND',`
_CODE_ENTRY_RE = re.compile(r"^\s*(?P<key>[A-Z][A-Z0-9_]*)\s*:\s*'(?P<value>[A-Z0-9_]+)'", re.MULTILINE)


def test_frontend_error_codes_match_backend():
    """前端错误码表与后端 `ErrorCode` 必须逐项相等。

    这是本次改造唯一的**跨端耦合点**：后端加码而前端没同步（或反之），
    前端的 `isApiError(e, 'XXX')` 会永远为假，降级逻辑静默失效。
    因此这里断言的是集合相等，而不是「前端的码后端都认」这种单向包含。
    """
    errors_ts = _FRONTEND_SRC / "services" / "errors.ts"
    if not errors_ts.exists():
        pytest.skip("前端源码目录不存在，跳过跨端守卫检查")

    table = _CODE_TABLE_RE.search(errors_ts.read_text(encoding="utf-8"))
    assert table is not None, "没在 errors.ts 里找到 BACKEND_ERROR_CODES 常量表"

    entries = list(_CODE_ENTRY_RE.finditer(table.group("body")))
    assert entries, "常量表解析出 0 个条目——正则需要跟着表结构一起更新"

    frontend_keys = {entry["key"] for entry in entries}
    frontend_values = {entry["value"] for entry in entries}
    backend_names = {code.name for code in ErrorCode}

    assert frontend_keys == backend_names, (
        f"前端键名与后端成员名不一致：前端独有 {frontend_keys - backend_names}，"
        f"后端独有 {backend_names - frontend_keys}"
    )
    assert frontend_values == _KNOWN_CODES, (
        f"前端码值与后端码值不一致：前端独有 {frontend_values - _KNOWN_CODES}，"
        f"后端独有 {_KNOWN_CODES - frontend_values}"
    )


# ============================================================================
# 不变量 3：前端不得再对中文文案做判断
# ============================================================================

# 一个「含中文的字符串字面量」（单引号 / 双引号 / 模板串）
_CJK_LITERAL = (
    r"(?:'(?:\\.|[^'\\\n])*[一-鿿](?:\\.|[^'\\\n])*'"
    r'|"(?:\\.|[^"\\\n])*[一-鿿](?:\\.|[^"\\\n])*"'
    r"|`(?:\\.|[^`\\])*[一-鿿](?:\\.|[^`\\])*`)"
)
# 字符串包含 / 定位 / 正则匹配类方法
_STRING_METHODS = r"(?:includes|indexOf|startsWith|endsWith|match|search|test)"
# 等值比较运算符；`===` 要排在 `==` 之前，否则会被短的那个先匹配掉
_EQUALITY = r"(?:===|!==|==|!=)"

# 三种形态：中文串作方法入参、中文串在比较运算符左侧、中文串在右侧，以及 switch-case
_CHINESE_JUDGEMENT_RES = [
    re.compile(r"\.\s*" + _STRING_METHODS + r"\s*\(\s*" + _CJK_LITERAL),
    re.compile(_CJK_LITERAL + r"\s*" + _EQUALITY),
    re.compile(_EQUALITY + r"\s*" + _CJK_LITERAL),
    re.compile(r"case\s+" + _CJK_LITERAL),
]


def _find_chinese_judgements(text: str) -> list[str]:
    """找出「拿中文串做判断」的代码行。

    只认**中文串本身就是判断对象**的写法：`x.includes('中文')`、`x === '中文'`。
    像 `if (stage === 'done') return '正在审核'` 这种——比较的是 ASCII 枚举值、
    中文只是同行的返回值——不算，否则全是误报。
    """
    offenders: list[str] = []
    for pattern in _CHINESE_JUDGEMENT_RES:
        for match in pattern.finditer(text):
            line_start = text.rfind("\n", 0, match.end()) + 1
            line_end = text.find("\n", match.end())
            offenders.append(text[line_start : line_end if line_end > 0 else len(text)].strip())
    return offenders


def test_chinese_judgement_detector_is_not_vacuous():
    """自检：上面的扫描规则必须真能抓到改造前的那种写法。

    一条「永远不报错」的守卫等于没有守卫——正则写错、表结构变了都会让它悄悄失明，
    所以用一段已知的坏代码先证明它是睁着眼的。
    """
    legacy = "if (error.message.includes('会话不存在')) { retry() }"
    assert _find_chinese_judgements(legacy), "扫描规则漏掉了最典型的中文文案判断"


def test_frontend_makes_no_chinese_string_judgement():
    """前端源码里不得再出现对中文文案的判断。

    测试文件（`__tests__`）除外——它们需要拿具体的中文串来断言展示文案。
    """
    if not _FRONTEND_SRC.exists():  # 单独跑 backend 时前端可能不在
        pytest.skip("前端源码目录不存在，跳过跨端守卫检查")

    offenders: list[str] = []
    for path in _FRONTEND_SRC.rglob("*"):
        if path.suffix not in {".ts", ".vue"} or "__tests__" in path.parts:
            continue
        offenders.extend(
            f"{path.relative_to(_FRONTEND_SRC)} → {line}"
            for line in _find_chinese_judgements(path.read_text(encoding="utf-8"))
        )

    assert not offenders, "前端又在拿中文文案做判断了：" + "；".join(offenders)


# ============================================================================
# 不变量 4：接口层不得再用裸 HTTPException
# ============================================================================


def test_api_layer_raises_no_bare_http_exception():
    """`api/` 下不得出现 `HTTPException`。

    它渲染出的响应体只有 `detail`、没有 `code`，等于在契约上开了一个洞：
    前端拿到的是 `INTERNAL_ERROR` 降级，错误分类信息在传输途中就丢了。
    统一抛 `AppError`（见 `core/errors.py`）才能走自定义处理器补上 `code`。

    这里扫的是**整份文本**，注释里提到这个词同样会被计入——规则简单才不会被绕过。
    """
    offenders = [
        f"{path.name}:{line_no} → {line.strip()}"
        for path in sorted(_BACKEND_API_SRC.glob("*.py"))
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "HTTPException" in line
    ]

    assert not offenders, "接口层又出现了裸 HTTPException：" + "；".join(offenders)
