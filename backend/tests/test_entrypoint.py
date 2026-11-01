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

所以把它们变成每次 `pytest` 都会亮的红灯（issue #4）。
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import tomllib

from fastapi import FastAPI

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _BACKEND_ROOT / "src" / "backend"

# 注册路由的装饰器名。`api_route` / `route` 也能注册路径，一并算上。
_ROUTE_DECORATORS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "api_route", "route"})

_HEALTH_PATH = "/health"


def _console_entry_point(name: str = "backend") -> str:
    """读打包入口点声明（`[project.scripts]` 是「怎么启动」的唯一真源）。"""
    data = tomllib.loads((_BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["scripts"][name]


def _resolve_entry_point(target: str) -> object:
    """按 `模块:属性` 解析入口点（与 console_scripts 的解析规则一致）。"""
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


# ---------------------------------------------------------------------------
# 切片一 · 打包入口点必须解析到真实存在的应用
# ---------------------------------------------------------------------------


def test_console_entry_point_resolves_to_the_real_application() -> None:
    """`[project.scripts]` 指向的属性必须真的存在，且就是被 uvicorn 加载的那个 app。

    只断言「字符串等于某个值」是空的：把入口点改对之后它一样可能是另一个对象。
    这里真的按 `模块:属性` 解析一遍，再与 `backend.main:app` 比同一性——
    「启动哪个应用」从此只有一个答案。
    """
    target = _console_entry_point()

    assert ":" in target, f"入口点必须写成 模块:属性 才能被 console_scripts 解析：{target!r}"
    resolved = _resolve_entry_point(target)

    assert isinstance(resolved, FastAPI), f"入口点 {target!r} 解析到 {type(resolved).__name__}，不是 ASGI 应用"

    from backend.main import app as running_app

    assert resolved is running_app, f"入口点 {target!r} 解析到的不是 backend.main:app"


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
