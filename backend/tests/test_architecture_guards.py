"""架构守卫（v6 / T16.12）：把纪律变成**每次跑测试都会亮的红灯**。

## 为什么这些断言值得存在

迁移到 SQLModel 之后，收益来自三件事：连接只由 Engine 给、事务边界只在
`session_scope()`、仓库只能拿着 `Session` 干活。这三件事**写错了都不会报错**——
程序照跑、接口照返回 200，只是某天在另一台机器上出现「指针指向没写进去的版本」
或者「池子被占满、接口全超时」。

所以它们必须由机器守，而不是靠 review 时记住。

## 编号与归属

| # | 断言 | 在本文件？ |
| --- | --- | --- |
| C1 | `repositories/` 无 `.commit()` / `.rollback()` | 是 |
| C2 | `repositories/` 无 SQL 字面量、无 `import sqlite3` | 是 |
| C3 | 仓库方法是实例方法，`__init__` 首参是 `Session` | 是 |
| C4 | `async def` 里 `session_scope()` 的块内不许 `await` | 是 |
| C5 | 库的列集合 == 模型字段集合 | 否 → `test_schema_parity.py::test_every_table_is_modelled` |
| C6 | 基线建出来的库 == 模型建的库（语义快照） | 否 → `test_schema_migration.py::test_baseline_produces_exactly_the_model_schema` |
| C7 | 布尔列集合 == 白名单 | 否 → `test_schema_parity.py::test_boolean_columns_match_whitelist` |
| C8 | `create_all` 只出现在测试与「用模型建临时库」那一处 | 是 |
| C9 | 旧迁移链（`user_version` 20 步）不许回来 | 是 |

C5/C6/C7 需要真的建一个库再逐列对拍，它们与各自的夹具（临时目录、空库、模型快照）
绑在一起；搬到这里只会把夹具也搬一遍。**归属不同，判据同级**——都在默认 `pytest` 里跑。

## 为什么用 AST 而不是正则

`"commit("` 这种文本匹配会被注释、docstring 和 `commit_generated` 这类名字误伤。
**误伤的守卫最后一定会被人关掉**，而关掉之后它连"存在"都算不上。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _BACKEND_ROOT / "src" / "backend"
_REPO_DIR = _SRC / "repositories"

# SQL 语句的「开头」。只匹配真正像语句的串，避免把 `"SELECT * 太慢"` 这种说明文字算进来。
_SQL_STATEMENT = re.compile(
    r"(?is)^\s*(select\s|insert\s+into\s|update\s+\w+\s+set\s|delete\s+from\s"
    r"|create\s+(table|index|unique)\s|alter\s+table\s|drop\s+(table|index)\s|pragma\s)"
)

# 仓库层的文件（不含 `__init__.py`：它只做再导出，没有类）
_REPO_FILES = sorted(path for path in _REPO_DIR.glob("*.py") if path.name != "__init__.py")


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstring_values(tree: ast.Module) -> set[int]:
    """收集所有 docstring 字面量的 id。

    AST 里 docstring 就是一条 `Expr(Constant(str))`，与「SQL 字面量」节点类型相同；
    必须先把它们摘出去，否则模块/类/函数的说明文字会被当成违规。
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


# ---------------------------------------------------------------------------
# C1 · 仓库层不许出现事务边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _REPO_FILES, ids=lambda p: p.name)
def test_c1_repositories_never_commit_or_rollback(path: pathlib.Path) -> None:
    """仓库里出现 `.commit()` / `.rollback()` 时转红。

    为什么这条最重要：一旦仓库自己提交，「删下游 + 插版本 + 回填指针」这种跨表原子
    会在中途被截断，而**没有任何报错**——留下的正是「指针指向一个没写进去的版本」。
    """
    tree = _parse(path)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"commit", "rollback"}:
                offenders.append(f"第 {node.lineno} 行 .{node.func.attr}()")
    assert offenders == [], (
        f"{path.name} 里出现了事务边界操作。仓库层不许提交/回滚，"
        f"事务只属于 session_scope()：{offenders}"
    )


# ---------------------------------------------------------------------------
# C2 · 仓库层不许自己开连接、不许手写 SQL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _REPO_FILES, ids=lambda p: p.name)
def test_c2_repositories_have_no_sqlite3_import(path: pathlib.Path) -> None:
    """`import sqlite3` 意味着有一条连接绕过了 Engine 的池。

    绕过池不是「性能问题」，而是**竞态**问题：连接在运行期新建时，多线程首次映射
    WAL 的 `-shm` 会拿到 `SQLITE_READONLY`（不是 `SQLITE_BUSY`，所以 `busy_timeout`
    救不了它）。Engine 的固定池 (`max_overflow=0`) 存在的理由就是让这件事没有发生的时机。
    """
    tree = _parse(path)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [alias.name for alias in node.names if alias.name == "sqlite3"]
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            offenders.append(f"from sqlite3 import {', '.join(a.name for a in node.names)}")
    assert offenders == [], f"{path.name} 还在直接使用 sqlite3：{offenders}"


@pytest.mark.parametrize("path", _REPO_FILES, ids=lambda p: p.name)
def test_c2_repositories_have_no_sql_literals(path: pathlib.Path) -> None:
    """仓库里不许出现 SQL 语句字面量（docstring 除外）。

    判据是「长得像一条 SQL 语句的字符串」，不是「含 select 这个词」：
    说明文字里写「以前的 SELECT * 很慢」是正常的，而那类误伤会让守卫被关掉。
    """
    tree = _parse(path)
    docstrings = _docstring_values(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and _SQL_STATEMENT.match(node.value)
        ):
            head = node.value.strip().splitlines()[0][:60]
            offenders.append(f"第 {node.lineno} 行：{head!r}")
    assert offenders == [], (
        f"{path.name} 里出现了手写 SQL。表达式写不出来时先问「是不是模型缺了什么」，"
        f"不要退回字符串：{offenders}"
    )


# ---------------------------------------------------------------------------
# C3 · 仓库只能是「拿着 Session 的实例方法」
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _REPO_FILES, ids=lambda p: p.name)
def test_c3_repository_classes_are_session_bound(path: pathlib.Path) -> None:
    """每个 `*Repository` 类都必须 `__init__(self, session)`，公开方法首参是 `self`。

    这条守的是「有没有把仓库写成模块级函数」——模块级函数天然没有 `Session` 可用，
    于是自己开连接的动机就回来了。
    """
    tree = _parse(path)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    repo_classes = [node for node in classes if node.name.endswith("Repository")]
    assert repo_classes, f"{path.name} 里没有任何 *Repository 类（类清单：{[c.name for c in classes]}）"

    problems: list[str] = []
    for cls in repo_classes:
        init = next(
            (
                node
                for node in cls.body
                if isinstance(node, ast.FunctionDef) and node.name == "__init__"
            ),
            None,
        )
        if init is None:
            problems.append(f"{cls.name} 没有 __init__，拿不到 Session")
            continue
        params = [arg.arg for arg in init.args.args]
        if params[:2] != ["self", "session"]:
            problems.append(f"{cls.name}.__init__ 签名是 {params}，应为 (self, session)")

        for method in cls.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if method.name.startswith("_"):
                continue
            args = [arg.arg for arg in method.args.args]
            if not args or args[0] != "self":
                problems.append(f"{cls.name}.{method.name} 不是实例方法（参数 {args}）")
    assert problems == [], f"{path.name}：{problems}"


@pytest.mark.parametrize("path", _REPO_FILES, ids=lambda p: p.name)
def test_c3_module_level_functions_do_not_touch_the_session(path: pathlib.Path) -> None:
    """模块级函数可以存在（如 `decode_extra` 这种纯解码），但**不许碰会话**。

    允许纯函数是为了不必为一两个 JSON 解码helper 造一个类；不许碰会话是因为
    「谁都能拿 session 就写一句」正是仓库层语义散掉的开始。
    """
    tree = _parse(path)
    problems: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [arg.arg for arg in node.args.args]
        if "session" in params:
            problems.append(f"{node.name} 直接接收 session 参数")
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                if inner.func.attr in {"execute", "add", "flush", "scalar_one"}:
                    problems.append(
                        f"{node.name} 里调用了 .{inner.func.attr}()（模块级函数不该有会话）"
                    )
                    break
    assert problems == [], f"{path.name}：{problems}"


# ---------------------------------------------------------------------------
# C4 · session 不许跨 await
# ---------------------------------------------------------------------------


def _iter_sources() -> list[pathlib.Path]:
    return sorted(path for path in _SRC.rglob("*.py") if "__pycache__" not in path.parts)


def _is_session_scope(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "session_scope"
    )


def test_c4_session_scope_is_never_held_across_await() -> None:
    """`async def` 里 `with session_scope()` 的块内出现 `await` 时转红。

    池只有 `pool_size` 条连接。一个协程握着连接去等 LLM 流式输出，就把整条池占住了，
    表现是「接口全部超时、日志里没有任何错误」。这条规则以前是 G-DB4（守 `conn`
    不跨 await），现在守的是 session。
    """
    offenders: list[str] = []
    for path in _iter_sources():
        tree = _parse(path)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.AsyncFunctionDef,)):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.With):
                    continue
                if not any(_is_session_scope(item.context_expr) for item in node.items):
                    continue
                for stmt in node.body:
                    if any(isinstance(inner, ast.Await) for inner in ast.walk(stmt)):
                        offenders.append(f"{path.name}:{node.lineno}（在 {func.name} 里）")
                        break
    assert offenders == [], (
        "session_scope() 的块内出现了 await：连接会被这条协程长期占住，"
        f"池满之后全站接口超时。把 await 移到块外，或只把结果带进块内：{offenders}"
    )


# ---------------------------------------------------------------------------
# C8 · 建表只能由 Alembic 产生
# ---------------------------------------------------------------------------

# 允许出现 `create_all` 的位置。除测试之外只有一处：`core/schema.py::_models_snapshot`
# 用模型在**临时目录**里建一个库，作为 C6 对拍的「期望形状」。它不是生产建表路径。
_CREATE_ALL_ALLOWED = {
    "core/schema.py": "用模型在临时目录建「期望形状」，供 C6 语义对拍（不碰业务库）",
}


def test_c8_create_all_only_in_tests() -> None:
    """`SQLModel.metadata.create_all` 不许出现在生产建表路径上。

    生产建表只能由 Alembic 完成：`create_all` 既不会记版本、也无法表达「已有库怎么演进」，
    一旦它被用来建业务库，`alembic_version` 与现实结构就会永久分叉。

    ⚠️ 判据是**真的调用了** `.create_all(...)`（AST 里的调用节点），不是文本里出现过
    这个词：`core/models.py` 的模块说明里就写着「不要用 create_all 建业务库」，
    按文本匹配会把那句正确的提醒本身判成违规。
    """
    offenders: list[str] = []
    for path in _iter_sources():
        rel = path.relative_to(_SRC).as_posix()
        if rel in _CREATE_ALL_ALLOWED:
            continue
        tree = _parse(path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_all"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        "生产代码里调用了 create_all（建表只能由 Alembic 产生）。"
        f"若确属测试装置，请放到 tests/ 下：{offenders}"
    )


# ---------------------------------------------------------------------------
# C9 · 旧迁移链不许回来
# ---------------------------------------------------------------------------

# v6.1 删掉的两个文件。它们合起来是「`PRAGMA user_version` 的 20 步手写链」。
_BANNED_LEGACY_FILES = {
    "core/migrations.py": "v0 基线 + 20 步 user_version 链",
    "core/legacy_migrations.py": "旧链的 Engine 适配器",
}

# 旧链的专属标识。出现在生产代码里就说明有人把第二套版本真相请回来了。
_LEGACY_NEEDLES = {"SCHEMA_SQL", "init_schema"}


def test_c9_legacy_migration_chain_stays_deleted() -> None:
    """旧迁移链不许回来（v6.1 删除）：文件不存在，生产代码也不再提它。

    为什么这条值得一条守卫：两套版本真相并存时，谁也说不清哪个库是「最新」——
    Alembic 会说已是最新，`user_version` 会说还差三步，而两边都「自洽」。
    那时唯一的判据就只剩「上线之后有没有炸」，而这正是我们要避免的。

    判据分三层：

    1. 两个文件不存在；
    2. 生产代码里没有 `SCHEMA_SQL` / `init_schema` 的**引用**；
    3. 生产代码里没有 `PRAGMA user_version` 这类字符串（docstring 除外）。

    旧版本库（v20 之前）现在会撞上 `SchemaMismatchError` 拒绝启动，而不是被自动升级。
    """
    present = [
        f"{rel}（{why}）" for rel, why in _BANNED_LEGACY_FILES.items()
        if (_SRC / rel).exists()
    ]
    assert present == [], f"旧迁移链的文件又出现了：{present}"

    offenders: list[str] = []
    for path in _iter_sources():
        rel = path.relative_to(_SRC).as_posix()
        tree = _parse(path)
        docstrings = _docstring_values(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in _LEGACY_NEEDLES:
                offenders.append(f"{rel}:{node.lineno} 引用了 {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in _LEGACY_NEEDLES:
                offenders.append(f"{rel}:{node.lineno} 引用了 .{node.attr}")
            elif isinstance(node, ast.alias) and node.name.split(".")[-1] in _LEGACY_NEEDLES:
                offenders.append(f"{rel}:{node.lineno} 导入了 {node.name}")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and "user_version" in node.value
            ):
                offenders.append(f"{rel}:{node.lineno} 出现了 user_version 字符串")
    assert offenders == [], (
        "生产代码里又出现了旧迁移链的痕迹（第二套版本真相）。"
        f"版本的唯一真源是 alembic_version：{offenders}"
    )
