"""守卫：v8 清理（issue #3）删掉的死符号不许再悄悄长回来。

为什么值得一条用例
------------------
这一批符号的判据是「**全仓零提及**」——它们没有调用方，也没有可观察行为。删掉不写用例，
退货时同样没人发现：既有 1000+ 条用例不会红，`ruff` 也不会红（`F401` 只管 import，
管不到「定义了但没人用」的函数与常量）。

于是它们会以「读起来像对外接口、其实谁都不调」的形式重新长出来，而这正是本清单
要消灭的那种阅读成本。这条用例是唯一的报警点：**名字回来就红**。

⚠️ 反向的代价说清楚：这条用例拦的是「重新定义同名符号」。将来真有人需要同名能力
（例如某个 `has_turns` 确实有人调用），正确做法是**连这条用例里对应的一行一起改**，
而不是把它注释掉——否则守卫会慢慢变回「永远不红」。

判据与清单来源：`docs/architecture/v8/ARCHITECTURE.md 附录 A` §2（A6 / A7）。
"""

from __future__ import annotations

import importlib

import pytest

# (模块, 类名或 None, 已删除的符号名)。类名非空表示该符号是类方法。
_REMOVED_SYMBOLS = [
    # A6：被 `delete_solution_and_cancel_jobs` 内联重写覆盖的取消函数
    ("backend.services.job_service", None, "cancel_for_solution"),
    # A7：10 个全仓零提及的符号
    ("backend.core.chat_kinds", None, "normalize_stage"),
    ("backend.core.chat_kinds", None, "is_valid_role"),
    ("backend.core.crypto", None, "master_key_exists"),
    ("backend.core.secrets", None, "is_present"),
    ("backend.core.engine", None, "current_engine"),
    ("backend.core.runtime_config", None, "config_service_window"),
    ("backend.core.runtime_config", None, "input_ceiling_for"),
    ("backend.repositories.chat_session_repo", "ChatSessionRepository", "clear_for_solution"),
    ("backend.repositories.chat_turn_repo", "ChatTurnRepository", "has_turns"),
    ("backend.core.schema", None, "_PLACEHOLDER_URL"),
]


@pytest.mark.parametrize(
    ("module_name", "class_name", "symbol"),
    _REMOVED_SYMBOLS,
    ids=[f"{module_name.split('.')[-1]}.{symbol}" for module_name, _, symbol in _REMOVED_SYMBOLS],
)
def test_dead_symbol_stays_deleted(module_name: str, class_name: str | None, symbol: str) -> None:
    """符号必须已经不存在（A6 / A7 的删除结果不许被悄悄引回）。"""
    target = importlib.import_module(module_name)
    if class_name is not None:
        target = getattr(target, class_name)
    assert not hasattr(target, symbol), (
        f"{module_name}{'.' + class_name if class_name else ''} 又长回了死符号 `{symbol}`："
        "它全仓零调用。要么接上真实调用方，要么按 附录 A §1 的三条判据重新裁决。"
    )
