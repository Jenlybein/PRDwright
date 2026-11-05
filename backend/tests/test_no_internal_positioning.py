"""守卫：生成任务用例只跨「造单 → 登记 → 派发 → 订阅」主接缝断言（票 #39）。

为什么值得一条用例
------------------
内部摆位用例把断言架在「运行时长什么样、收尾函数怎么调、登记表怎么摆」上：
内部实现一重排，用例就得跟着改，而用户可见行为其实没变。归位之后必须有一道
报警点，否则新用例又会慢慢把零件名抄回来——`ruff` 管不到这种耦合，`pytest`
既有 1000+ 条用例也不会红。

用 AST 找**真实调用 / 下标**，不做纯文本 grep：用例名里带 `_write_document`、
docstring 里提到 `_RUNTIMES`、夹具里的 `_RUNTIMES.clear()` 与「空登记表」断言
都不属于摆位，不该被误判。

允许保留的两类写法（都不是摆位）：
- 夹具清理：`_RUNTIMES.clear()` / `_TASKS.clear()` / `_RUNTIMES.pop(...)`；
- 「没有任务被派发」的空登记表断言：`_RUNTIMES == {}`。

旧入口已随票 #40 删除，不设兼容期；本守卫只管「用例不摆内部状态」，
主接缝覆盖由 `test_cleanup_dead_code.py` 的覆盖守卫兜底。
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent

# 内部零件名：测试里出现这些**调用**，就是把断言架在了内部实现上。
_FORBIDDEN_CALLS = frozenset(
    {
        "JobRuntime",
        "_finalize",
        "_apply_event",
        "_snapshot_event",
        "_db_snapshot_event",
        "_write_document",
        "_write_chat_turn",
        "_draft",
    }
)

# 进程内运行时登记表：读 / 写下标都算探内部。
_FORBIDDEN_REGISTRY = "_RUNTIMES"


def _called_name(node: ast.AST) -> str | None:
    """取调用目标的末级名字（`js._apply_event` → `_apply_event`）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _violations(path: Path) -> list[str]:
    """扫一份测试文件，返回所有内部摆位的行号与说明。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _called_name(node.func)
            if name in _FORBIDDEN_CALLS:
                found.append(f"{path.name}:{node.lineno} 直调内部零件 {name}(")
        elif isinstance(node, ast.Subscript):
            name = _called_name(node.value)
            if name == _FORBIDDEN_REGISTRY:
                found.append(f"{path.name}:{node.lineno} 探内部登记表 {name}[...]")
    return found


def test_job_tests_assert_only_through_public_ticket_seam() -> None:
    """测试不得构造裸运行时、不得摆内部登记表、不得直调收尾 / 快照 / 落点零件。"""
    violations: list[str] = []
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        violations.extend(_violations(path))
    assert violations == [], (
        "以下用例仍把断言架在内部摆位上；请改走 "
        "build_job_ticket → register_job_ticket → start_registered_job → open_stream：\n"
        + "\n".join(violations)
    )
