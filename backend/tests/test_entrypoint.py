"""启动路径只有一条：应用对象与 `/health` 都必须独一无二。

## 为什么这几条值得存在

这个仓库一度有两个「能启动」的答案：`backend/src/backend/__init__.py` 里建了第二个
`FastAPI()` 并自带一条 `/health`，而真正被 uvicorn 加载的是 `backend/main.py` 里的那个；
打包入口点 `[project.scripts]` 又指向 `backend:main`——一个在包里**根本不存在**的属性
（`import backend` 之后 `hasattr(backend, "main")` 是 False）。

这三件事都不会在跑测试时自己暴露：

- 第二个 app 无人引用，所以没有任何用例会因为它的存在而变红；它却会让人以为「应用定义
  在包的 `__init__` 里」，照着它去加路由就写进了一个永远不会被启动的对象；
- 坏入口点只在**真的按脚本启动**时才报 `AttributeError`，而本地与部署都走
  `uvicorn backend.main:app`，于是它一路没人碰；
- 两条 `/health` 的语义不同（一条只回 `{"status":"ok"}`），若哪天有人把公开路径指到
  那一条，探活看似正常但三项体检全丢。

第二层坑在修完之后才浮出来：把入口点改成 `backend.main:app`，属性确实存在了，但
console script 会**零参调用**它，而 FastAPI 对象的 `__call__(scope, receive, send)`
要三个参数，于是 `uv run backend` 变成 `TypeError`。根因不是值写错，而是**把 web 应用
当成了可执行命令**——所以最终把那块声明整个删掉，由下面的用例钉住「别再把它加回来」，
并对任何新命令做一次零参可调用性检查。

所以把它们变成每次 `pytest` 都会亮的红灯（issue #4、后续收口）。

## 切片四为什么也在这里

探针是「启动路径」的延伸：容器起没起来、现在能不能接活，是同一件事的两个阶段。
而且它们的失效方式与前面的坑同源——**看起来有探针，其实没有判断力**：
`/health` 三项体检全红也恒返回 200，编排器拿它当探针等于永远健康。
所以「生死恒 200 / 就绪不 ok 就 503」这条分工由本文件守（issue #12）。
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import inspect
import pathlib
import tomllib

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine

from backend.core.config import get_settings

from conftest import write_skill_package

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _BACKEND_ROOT / "src" / "backend"

# 注册路由的装饰器名。`api_route` / `route` 也能注册路径，一并算上。
_ROUTE_DECORATORS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "api_route", "route"})

_HEALTH_PATH = "/health"


def _declared_scripts() -> dict[str, str]:
    """读打包声明的命令（`[project.scripts]`）。可能整块不存在，返回空表。"""
    data = tomllib.loads((_BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return dict(data.get("project", {}).get("scripts", {}))


def _resolve_entry_point(target: str) -> object:
    """按 `模块:属性` 解析入口点（与 console_scripts 的解析规则一致）。"""
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


# ---------------------------------------------------------------------------
# 切片一 · 不要把「web 应用」声明成「可执行命令」
# ---------------------------------------------------------------------------


def test_no_console_script_declared_for_the_backend() -> None:
    """`[project.scripts]` 里不该有 `backend`。

    这里连着踩过两次坑，值得记全：

    1. 值曾是 `backend:main`——包里不存在那个属性，真按脚本启动才炸 `AttributeError`；
    2. 改成 `backend.main:app` 之后**解析**通了，但 console script 会零参调用它，而
       FastAPI 对象的 `__call__(scope, receive, send)` 要三个参数 → `TypeError`。

    根因不是「值写错」，是**把 ASGI 应用当成了可执行命令**。应用只由服务器加载
    （`uvicorn backend.main:app`）。删掉声明是唯一不会误导人的收法——留着它，下一个人
    （或下一个 agent）照 `pyproject.toml` 敲命令，就会得到一个莫名其妙的报错。
    """
    scripts = _declared_scripts()

    assert "backend" not in scripts, (
        f"`[project.scripts]` 不该声明 backend 命令（当前 {scripts.get('backend')!r}）："
        "FastAPI 应用不是零参可调用对象，启动请用 `uvicorn backend.main:app`"
    )


def test_any_declared_script_is_actually_invocable() -> None:
    """若将来确实要声明命令，它必须能被零参调用——否则又是一条死命令。

    这条不是「断言现在是空的」（那样一有合法命令就红），而是**对所有现存命令生效**：
    解析 `模块:属性` 之后用 `signature().bind()` 试一次零参绑定。
    它同时覆盖了旧坑（属性不存在 → `AttributeError`）和新坑（ASGI 应用 → `TypeError`）。
    """
    for name, target in _declared_scripts().items():
        assert ":" in target, f"命令 {name!r} 必须写成 模块:属性 才能被 console_scripts 解析：{target!r}"
        resolved = _resolve_entry_point(target)

        try:
            inspect.signature(resolved).bind()
        except TypeError as exc:  # 需要参数 = 不能被当成命令
            pytest.fail(
                f"命令 {name!r} 指向 {target!r}，但它不能零参调用（{exc}）："
                "console script 会直接调用它，这条命令一敲就报错"
            )


# ---------------------------------------------------------------------------
# 切片二 · 包里不许再藏第二个应用
# ---------------------------------------------------------------------------


def test_backend_package_exposes_no_second_application() -> None:
    """`import backend` 不该顺手造出一个 FastAPI 应用。

    `backend` 是命名空间根，不是可启动的应用：应用只有一个实例，住在 `backend.main`。
    包级再挂一个 `app`，等于给「启动哪个」第二个答案，而那个答案没有任何用例覆盖。
    """
    import backend

    apps = [name for name, value in vars(backend).items() if isinstance(value, FastAPI)]

    assert apps == [], f"包 `backend` 里出现了第二个 FastAPI 应用：{apps}（应用只应有 backend.main:app 一处）"


# ---------------------------------------------------------------------------
# 切片三 · `/health` 只剩一处定义
# ---------------------------------------------------------------------------


def _health_route_registrations(tree: ast.Module) -> list[int]:
    """返回在一棵 AST 里注册 `/health` 的函数定义行号。"""
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            func = deco.func
            if not (isinstance(func, ast.Attribute) and func.attr in _ROUTE_DECORATORS):
                continue
            if not deco.args:
                continue
            first = deco.args[0]
            if isinstance(first, ast.Constant) and first.value == _HEALTH_PATH:
                lines.append(node.lineno)
    return lines


def test_health_route_detector_is_not_vacuous() -> None:
    """自检：扫描规则必须真能认出 `/health` 的注册写法。

    一条永远返回空列表的扫描等于没有扫描，所以先用一段已知的注册代码
    证明它睁着眼——避免规则写错却静默失明。
    """
    sample = ast.parse(
        '@router.get("/health")\nasync def health() -> dict:\n    return {}\n'
    )
    assert _health_route_registrations(sample) == [2]

    # 别的路径不该被误报
    other = ast.parse('@router.get("/healthz")\nasync def healthz() -> dict:\n    return {}\n')
    assert _health_route_registrations(other) == []


def test_health_route_is_registered_exactly_once() -> None:
    """全仓只允许一处 `/health` 注册。

    两条 `/health` 的语义并不相同：真应用那条报三项体检（`ok` / `degraded`），
    被删掉那条只回一句 `{"status": "ok"}`。留着一个「长得像体检、其实什么都没查」的
    入口，排障时就会照着它得出「服务正常」的结论。
    """
    hits: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line in _health_route_registrations(tree):
            hits.append(f"{path.relative_to(_SRC).as_posix()}:{line}")

    assert len(hits) == 1, f"`/health` 应有且仅有一处定义，实际 {len(hits)} 处：{hits}"
    assert hits[0].startswith("api/health.py:"), f"`/health` 应在 api/health.py 定义，实际在 {hits[0]}"


# ---------------------------------------------------------------------------
# 切片四 · 两条探针：生死恒 200，就绪不 ok 就 503
# ---------------------------------------------------------------------------


def _read_only_engine(db_file: pathlib.Path):
    """造一个「连得上、读得出、写不进」的 Engine。

    用 SQLite 的 `mode=ro` URI 打开同一个库文件，故障就精确落在**写**这一步：
    连接成功、查询成功，只有写会报 `attempt to write a readonly database`。
    这正是「库可写」这一项要抓的状态——「连不上」与「读得挺好、一写就废」是两种病，
    前者详情端点已经在报，后者只有写一次才知道。

    直接 `create_engine` 而不走 `build_engine`：后者会按 URI 里的 `file:` 前缀去推算
    目录（那是生产路径该管的事），这里只需要一条读得动、写不进的连接。
    """
    url = f"sqlite+pysqlite:///file:{db_file.as_posix()}?mode=ro&uri=true"
    return create_engine(url, connect_args={"timeout": 10, "check_same_thread": False})


@contextlib.contextmanager
def _fake_read_only_database(monkeypatch):
    """把探针用的 Engine 换成只读库，退出时还原。

    替换的是**依赖本身**（库），不是探针逻辑：探针照常借连接、照常执行写语句，
    失败来自 SQLite 真实的只读拒绝。
    """
    from backend.api import health

    engine = _read_only_engine(get_settings().db_file)
    monkeypatch.setattr(health, "get_engine", lambda: engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_healthz_always_returns_200(client):
    """生死探针只表示「进程活着」：固定 200 + 固定响应体，不查任何依赖。"""
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_healthz_stays_200_while_dependencies_are_broken(client, monkeypatch, tmp_path):
    """依赖坏到「接不了活」时，生死探针仍然 200——这就是它存在的意义。

    同时断言就绪探针在同一时刻说「没好」（503）：两条探针的分工正是这一对断言，
    只测其中一条会让「改错了却仍然绿」变得可能（例如有人把依赖检查搬进生死探针）。
    """
    # 技能目录指到一个不存在的目录：部署漏打包 skills/ 的现场
    monkeypatch.setenv("PRDWRIGHT_SKILLS_DIR", str(tmp_path / "no-skills-here"))

    with _fake_read_only_database(monkeypatch):
        live = client.get("/healthz")
        ready = client.get("/readyz")

    assert live.status_code == 200, "生死探针不许因为依赖坏了就返回非 200"
    assert live.json() == {"ok": True}
    assert ready.status_code == 503, "依赖坏掉时就绪探针必须说「没好」"


def test_readyz_returns_200_when_database_is_writable_and_skills_load(temp_skills_root, client):
    """两项都正常时就绪探针 200。

    夹具顺序有意义：`temp_skills_root` 先把技能根目录指到临时目录，
    `client` 才会带着这份配置走进 lifespan。
    """
    write_skill_package(temp_skills_root, "prd-generator")

    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert set(body["checks"]) == {"database", "skills"}
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["skills"]["ok"] is True
    # 就绪探针刻意不查模型供应商：那一项要发外部请求，不适合被高频触发的探针（见 api/health.py）
    assert "llm" not in body["checks"]


def test_readyz_flags_unavailable_skills(temp_skills_root, client):
    """技能包不在位时 503，且响应体指出失败的是**技能**这一项、原因是「一个包都没有」。

    `temp_skills_root` 只把根目录指到空目录、不往里放包——正是「部署时漏打包 skills/」
    的现场。此时数据库那一项仍然通过，所以这条断言同时证明了「分开报」。
    """
    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["skills"]["ok"] is False
    assert "未发现任何技能包" in body["checks"]["skills"]["detail"]
    assert body["checks"]["database"]["ok"] is True


def test_readyz_flags_unwritable_database(temp_skills_root, client, monkeypatch):
    """库读得出来、写不进去时 503，且响应体指出失败的是**数据库**这一项。

    这一项与详情端点的 `SELECT 1` 不是一回事：只读库在 `SELECT 1` 下完全健康，
    但它存不了方案、也写不了任务——「能不能接活」必须问写。
    """
    write_skill_package(temp_skills_root, "prd-generator")

    with _fake_read_only_database(monkeypatch):
        response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["database"]["ok"] is False
    # 公开路径只给异常类型，不吐路径等内部细节（与 /health 同一条纪律）
    assert "不可写" in body["checks"]["database"]["detail"]
    # 技能这一项是好的：失败项必须能被指出来，否则探针只等于一句「没好」
    assert body["checks"]["skills"]["ok"] is True


def test_probe_endpoints_do_not_require_login(temp_skills_root, client):
    """两条探针都在免登录白名单里：没有 Cookie 也能拿到结论。

    探针被鉴权拦成 401 时，编排器会把它读成「服务不可用」——一个「配了探针比没配还糟」
    的现场。所以这里既发真实请求（无登录态），也直接查白名单函数：
    请求断言守着行为，白名单断言让「谁把它挪出白名单」在 diff 里显形。
    """
    from backend.api.deps import is_public_path

    write_skill_package(temp_skills_root, "prd-generator")

    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200

    assert is_public_path("/healthz")
    assert is_public_path("/readyz")


def test_detail_health_endpoint_contract_is_unchanged(temp_skills_root, client):
    """详情端点的契约一个字都没变：三项体检、恒 200、`status` 两档。

    技能目录空着（本用例没放包）正好用来钉住「降级也照样 200」：把状态码打成失败会让
    编排器踢掉一个其实还能降级运行的容器——那件事现在归 `/readyz`，不归这里。
    """
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "checks"}
    assert set(body["checks"]) == {"database", "llm", "skills"}
    assert body["checks"]["skills"]["ok"] is False
    assert body["status"] == "degraded"
