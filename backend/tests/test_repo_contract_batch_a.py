"""批 A 仓库契约的静态守卫（v6 / T16.6）。

这五個仓库从「`@staticmethod` + 手写 sqlite3 + 自己 commit」迁成了「绑定 `Session` 的
实例方法」。迁移的收益（事务边界集中到 `session_scope()`）完全依赖三件事，
而这三件事**写错了都不会报错**：

1. 文件里若还留着 `import sqlite3`，说明有函数绕过了 SQLModel 直接开连接——
   那条连接不受 Engine 的池约束，等于在池子外面又长出一条；
2. 仓库里若出现 `.commit()` / `.rollback()`，事务边界就从服务层漏到了仓库层：
   服务层想「三个仓库一起回滚」时会发现前半截已经被提交了，而**没有任何报错**；
3. 若还留着 `lease(`，说明有函数在用旧的手写连接上下文，与新会话并存两套语义。

三条都用 AST 判，不用正则：
`"commit("` 这种文本匹配会被注释、docstring 和 `commit_generated` 之类的名字误伤，
而**误伤的守卫最终会被人关掉**——那比没有守卫更糟。

这组断言守的是「未来新写的仓库代码」，不是「这次改完人眼过一遍」。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

# 批 A 的五个文件（用 `__file__` 定位，不依赖 cwd）
_REPO_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "backend" / "repositories"

_BATCH_A = (
    "log_repo.py",
    "chat_session_repo.py",
    "chat_turn_repo.py",
    "quota_repo.py",
    "account_repo.py",
)

# 仓库层必须继承的三个类名（按文件名映射）。
# 单独断言它，是因为「有人新建了一个同名的模块级函数」最容易在 review 里滑过去：
# 模块级函数天然没有 `Session`，写出来的第一件事就是自己开连接。
_EXPECTED_CLASS = {
    "log_repo.py": "LogRepository",
    "chat_session_repo.py": "ChatSessionRepository",
    "chat_turn_repo.py": "ChatTurnRepository",
    "quota_repo.py": "QuotaRepository",
    "account_repo.py": "AccountDataRepository",
}


def _parse(name: str) -> ast.Module:
    """解析一个仓库文件。"""
    path = _REPO_DIR / name
    assert path.exists(), f"仓库文件不见了：{path}"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("name", _BATCH_A)
def test_repository_does_not_import_sqlite3(name: str) -> None:
    """① 不许再 `import sqlite3`：连接只由 Engine 提供。"""
    tree = _parse(name)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [alias.name for alias in node.names if alias.name == "sqlite3"]
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            offenders.append(f"from sqlite3 import {', '.join(a.name for a in node.names)}")
    assert offenders == [], f"{name} 还在直接使用 sqlite3（连接不受连接池约束）：{offenders}"


@pytest.mark.parametrize("name", _BATCH_A)
def test_repository_never_commits_or_rolls_back(name: str) -> None:
    """② 不许出现 `.commit()` / `.rollback()`：事务边界只属于 `session_scope()`。

    用 AST 而不是文本匹配：`db.commit_generated(...)` 这类名字会被正则误伤，
    docstring 里写一句「不 commit」也会。误伤的守卫最后一定会被关掉。
    """
    tree = _parse(name)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in {"commit", "rollback"}:
            offenders.append(f"第 {node.lineno} 行 .{node.func.attr}()")
    assert offenders == [], (
        f"{name} 里出现了事务边界操作——仓库层不许提交/回滚，"
        f"否则服务层的多仓库事务会在这里被提前截断：{offenders}"
    )


@pytest.mark.parametrize("name", _BATCH_A)
def test_repository_does_not_use_legacy_lease(name: str) -> None:
    """③ 不许再用旧的 `lease(`：手写连接上下文与会话两套语义不能并存。"""
    tree = _parse(name)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "lease":
                offenders.append(f"第 {node.lineno} 行")
        elif isinstance(node, ast.Name) and node.id == "lease":
            offenders.append(f"第 {node.lineno} 行（名字引用）")
    assert offenders == [], f"{name} 还在用 lease()：{offenders}"


@pytest.mark.parametrize("name", _BATCH_A)
def test_repository_class_is_session_bound(name: str) -> None:
    """④ 仓库类必须接受 `Session`（`__init__(self, session)`），否则它只能自己开连接。

    这一条不重复上面三条：上面守的是「有没有偷偷开连接」，这条守的是
    「有没有把仓库写成模块级函数」——后者天然没有 `Session` 可用，
    于是绕开 `session_scope()` 的动机就回来了。
    """
    tree = _parse(name)
    expected = _EXPECTED_CLASS[name]
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    matched = [node for node in classes if node.name == expected]
    assert matched, f"{name} 里没有类 {expected}（类清单：{[c.name for c in classes]}）"

    init = next(
        (node for node in matched[0].body if isinstance(node, ast.FunctionDef) and node.name == "__init__"),
        None,
    )
    assert init is not None, f"{expected} 没有 __init__，拿不到 Session"
    params = [arg.arg for arg in init.args.args]
    assert params[:2] == ["self", "session"], f"{expected}.__init__ 的签名是 {params}，应当是 (self, session)"
