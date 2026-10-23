"""批 C 仓库契约的静态守卫（v6 / T16.8）。

`generation_job_repo.py` 从「`@staticmethod` + 手写 sqlite3 + 自己 commit」迁成了
「绑定 `Session` 的实例方法」。这是全站改动面最大的单个仓储（任务生命周期：
创建 / 顶替 / 取消 / 失败 / 收尾 / 重启回收），它的正确性依赖三件事，而这三件事
**写错了都不会报错**：

1. 文件里若还留着 `import sqlite3`，说明有函数绕过了 SQLModel 直接开连接；
2. 仓库里若出现 `.commit()` / `.rollback()`，事务边界就从服务层漏到了仓库层——
   「标记内存运行时 + 落库终态」这类组合会在中途被截断，且**没有任何报错**；
3. 若还留着 `lease(`，说明有函数在用旧的手写连接上下文，与新会话并存两套语义。

三条都用 AST 判（不用正则）：`commit(` 的文本匹配会被 `commit_generated` 之类的
名字误伤，而误伤的守卫最终会被人关掉。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_REPO_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "backend" / "repositories"

_BATCH_C = ("generation_job_repo.py",)

_EXPECTED_CLASS = {"generation_job_repo.py": "GenerationJobRepository"}


def _parse(name: str) -> ast.Module:
    path = _REPO_DIR / name
    assert path.exists(), f"仓库文件不见了：{path}"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("name", _BATCH_C)
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


@pytest.mark.parametrize("name", _BATCH_C)
def test_repository_never_commits_or_rolls_back(name: str) -> None:
    """② 不许出现 `.commit()` / `.rollback()`：事务边界只属于 `session_scope()`。"""
    tree = _parse(name)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in {"commit", "rollback"}:
            offenders.append(f"第 {node.lineno} 行 .{node.func.attr}()")
    assert offenders == [], (
        f"{name} 里出现了事务边界操作——仓库层不许提交/回滚，"
        f"否则「标记运行时 + 落库终态」会在中途被截断：{offenders}"
    )


@pytest.mark.parametrize("name", _BATCH_C)
def test_repository_does_not_use_legacy_lease(name: str) -> None:
    """③ 不许再用旧的 `lease(`。"""
    tree = _parse(name)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "lease":
                offenders.append(f"第 {node.lineno} 行")
        elif isinstance(node, ast.Name) and node.id == "lease":
            offenders.append(f"第 {node.lineno} 行（名字引用）")
    assert offenders == [], f"{name} 还在用 lease()：{offenders}"


@pytest.mark.parametrize("name", _BATCH_C)
def test_repository_class_is_session_bound(name: str) -> None:
    """④ 仓库类必须接受 `Session`（`__init__(self, session)`）。"""
    tree = _parse(name)
    expected = _EXPECTED_CLASS[name]
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    matched = [node for node in classes if node.name == expected]
    assert matched, f"{name} 里没有类 {expected}（类清单：{[c.name for c in classes]}）"

    init = next(
        (
            node
            for node in matched[0].body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        ),
        None,
    )
    assert init is not None, f"{expected} 没有 __init__，拿不到 Session"
    params = [arg.arg for arg in init.args.args]
    assert params[:2] == ["self", "session"], (
        f"{expected}.__init__ 的签名是 {params}，应当是 (self, session)"
    )
