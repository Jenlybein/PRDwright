"""守卫：清理掉的死符号不许再悄悄长回来。

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

判据与清单来源：`docs/architecture/ARCHITECTURE.md` §5.5「清理判据（三条同时成立才动）」（A6 / A7 沿用原清单编号）。
#40 追加：旧入口、15 行再导出块与实现私有名从 `job_service` 公开面整块退场，
`assert not hasattr` 形态不变；另有公开面 / 包外调用方 / 主接缝覆盖三道守卫。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

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
    # #35：`job_service` 顶部再导出块里全仓零调用的 10 个内部名字。
    # 本体仍在各自拆出模块内部使用，这里只拦「不许以 `job_service.X` 的形式回来」。
    ("backend.services.job_service", None, "_build_derived_from"),
    ("backend.services.job_service", None, "_document_kind"),
    ("backend.services.job_service", None, "_regenerates_whole_document"),
    ("backend.services.job_service", None, "_SectionLive"),
    ("backend.services.job_service", None, "_FLUSH_BYTES"),
    ("backend.services.job_service", None, "_FLUSH_INTERVAL_SECONDS"),
    ("backend.services.job_service", None, "_finalized_text"),
    ("backend.services.job_service", None, "_live_len"),
    ("backend.services.job_service", None, "_elapsed_ms_from_row"),
    ("backend.services.job_service", None, "_load_pending_items"),
]

# #40：删旧入口与整块再导出后，`job_service` 公开面不再有这些名字。
# 判据同 A6/A7：`assert not hasattr`，名字回来就红。主文件正文仍以裸名自用的
# 实现细节住在内部模块里，包内互引不算泄漏；`parse_usage` / `JobRuntime` 各归其位。
_JOB_SERVICE_REMOVED_SYMBOLS = [
    # 旧入口（#37/#38/#39 迁移完毕后删除，不设兼容期）及其独占私有函数
    "create_job",
    "dispatch",
    "_create_chat_job",
    # 再导出块（#35 删 10 行后剩 15 行，#40 整块删空）
    "_finalize",
    "_write_chat_turn",
    "_write_document",
    "_target_product_kind",
    "_RUNTIMES",
    "_TASKS",
    "JobRuntime",
    "_apply_event",
    "_draft",
    "_maybe_flush",
    "_run_summary_event",
    "_summary_data",
    "_db_snapshot_event",
    "_snapshot_event",
    "parse_usage",
    # 主文件自身的私有实现：对外不可见，包内互引走内部模块
    "_immutable_projection",
    "_mark_superseded",
    "_build_event_stream",
    "run_job",
    "_failure_event",
    "_resolve_skill_refs",
]

_REMOVED_SYMBOLS += [
    ("backend.services.job_service", None, symbol)
    for symbol in _JOB_SERVICE_REMOVED_SYMBOLS
]

# 包入口实测只露的 9 个正常动作（#5 主文件自身的 11 个删掉 `create_job` /
# `dispatch` 后的余下部分）；#36 新三步与 #37 等待在途各自住在
# `job_ticket` / `job_wait` 的公开面上，不硬凑规格里的「11 个」。
_JOB_SERVICE_PUBLIC_ACTIONS = frozenset(
    {
        "reap_orphans",
        "cancel_for_user",
        "cancel_job",
        "list_jobs",
        "get_job",
        "open_stream",
        "delete_solution_and_cancel_jobs",
        "cancel_in_flight",
        "running_runtimes",
    }
)


@pytest.mark.parametrize(
    ("module_name", "class_name", "symbol"),
    _REMOVED_SYMBOLS,
    ids=[f"{module_name.split('.')[-1]}.{symbol}" for module_name, _, symbol in _REMOVED_SYMBOLS],
)
def test_dead_symbol_stays_deleted(module_name: str, class_name: str | None, symbol: str) -> None:
    """符号必须已经不存在（A6 / A7 / #35 / #40 的删除结果不许被悄悄引回）。"""
    target = importlib.import_module(module_name)
    if class_name is not None:
        target = getattr(target, class_name)
    assert not hasattr(target, symbol), (
        f"{module_name}{'.' + class_name if class_name else ''} 又长回了死符号 `{symbol}`："
        "它全仓零调用。要么接上真实调用方，要么按 ARCHITECTURE.md §5.5 的三条判据重新裁决。"
    )


def test_job_service_public_surface_exposes_only_actions() -> None:
    """`from backend.services import job_service` 只能取到有限个正常动作。

    公开面不许夹带内部私有名：实现住在同目录的内部模块里，包外一律看不见。
    这条与上面的 `assert not hasattr` 互补——那条按名字点名，这条按形状兜底。
    """
    module = importlib.import_module("backend.services.job_service")
    assert set(getattr(module, "__all__", ())) == _JOB_SERVICE_PUBLIC_ACTIONS
    leaked = sorted(
        name
        for name in dir(module)
        if name.startswith("_") and not name.startswith("__")
    )
    assert leaked == [], f"公开面泄漏了内部私有名：{leaked}"
    for name in _JOB_SERVICE_PUBLIC_ACTIONS:
        assert callable(getattr(module, name)), f"公开动作 {name} 不可调用"


# 四条旧的非任务生成通道（#61 退役）。它们的**服务方法**仍被对应的生成任务复用，
# 死的是「路由 + 请求模型 + 前端封装」这一层：还挂在那里就等于同一条生成能力
# 有第二个入口，而 C-R3「输入只来自服务端」不该靠调用方自觉。
_RETIRED_GENERATION_ROUTES = (
    "/api/conversation/generate-prd-stream",
    "/api/conversation/generate-prd-from-summary-stream",
    "/api/conversation/generate-api-docs-stream",
    "/api/conversation/generate-prompts-stream",
)

# 退役后必须**还在**的服务入口：`_build_event_stream` 直接调，`prd` 任务另有
# 「分章节降级为整篇」的兜底调用。删路由不等于删能力（约束：不删业务服务方法）。
_SURVIVING_GENERATION_METHODS = (
    "generate_prd_stream",
    "generate_prd_from_summary_stream",
    "generate_api_docs_stream",
    "generate_prompts_stream",
)


def test_retired_generation_routes_are_gone_from_openapi() -> None:
    """四条旧直连通道不许再出现在 OpenAPI 里（#61 的退役结果）。

    为什么用 OpenAPI 而不是 `hasattr`：这四条是**路由**，它们的「存在」体现为
    OpenAPI 里的一条 path operation 与一组请求模型；文件里还留着一个同名函数却没挂
    router，同样是死代码。反过来，路由删干净时 OpenAPI 是唯一权威的清单。
    """
    from backend.main import app

    paths = set(app.openapi()["paths"])
    leaked = [route for route in _RETIRED_GENERATION_ROUTES if route in paths]
    assert leaked == [], (
        "旧的非任务生成通道又挂回来了："
        f"{leaked}；它们与 `POST /api/jobs` 是同一个能力的两个入口，"
        "按 ARCHITECTURE.md §5.5 三条判据应当只留任务通道。"
    )


def test_generation_service_methods_survive_route_retirement() -> None:
    """退役只删路由层：四个服务方法仍在（任务通道还在直接调它们）。

    这条与上一条成对：只有「路由没了」才可能是删漏了业务逻辑，只有「方法还在」
    也说明不了路由有没有清干净。两条一起才钉住「删路由不删能力」。
    """
    from backend.services import document_service

    missing = [
        name for name in _SURVIVING_GENERATION_METHODS if not callable(getattr(document_service, name, None))
    ]
    assert missing == [], f"退役误伤了服务方法（生成任务会当场跑不起来）：{missing}"


def _external_python_files() -> list[Path]:
    """包外调用方：接口层 / 启动流程 / 其它服务 / 全部测试。"""
    root = Path(__file__).resolve().parents[2] / "backend"
    paths = list((root / "src" / "backend" / "api").glob("*.py"))
    paths += list((root / "src" / "backend").glob("main.py"))
    paths += [
        root / "src" / "backend" / "services" / "admin_service.py",
        root / "src" / "backend" / "services" / "solution_service.py",
    ]
    paths += [
        path
        for path in (root / "tests").glob("*.py")
        if path.name != "test_cleanup_dead_code.py"
    ]
    return [path for path in paths if path.exists()]


def test_external_callers_do_not_cross_job_service_interface() -> None:
    """包外调用方不得从 `job_service` 取内部名 / 旧入口 / 再导出名。

    用 AST 找真实的 `job_service.X` 属性访问：注释与 docstring 里提到旧名不算越界，
    夹具清理改从 `job_runtime` 这类「真正所属处」引用也不算越过公开面。
    """
    violations: list[str] = []
    for path in _external_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "job_service"
                and node.attr not in _JOB_SERVICE_PUBLIC_ACTIONS
            ):
                violations.append(f"{path.name}:{node.lineno} job_service.{node.attr}")
    assert violations == [], (
        "以下调用方越过了 `job_service` 公开面（私有名请从真正所属的内部模块引用）：\n"
        + "\n".join(violations)
    )


def test_three_step_seam_coverage_survives_legacy_removal() -> None:
    """删掉 2 条旧对拍用例后，主接缝文件仍覆盖造单→登记→派发→订阅。"""
    path = Path(__file__).resolve().parent / "test_job_ticket_three_steps.py"
    source = path.read_text(encoding="utf-8")
    assert "job_service.create_job" not in source, "旧入口对拍用例没删干净"
    assert "job_service.dispatch" not in source, "旧入口对拍用例没删干净"
    for seam in (
        "build_job_ticket",
        "register_job_ticket",
        "start_registered_job",
        "open_stream",
    ):
        assert seam in source, f"主接缝覆盖丢了：{seam}"
    tree = ast.parse(source, filename=str(path))
    tests = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    assert len(tests) >= 15, f"主接缝用例数掉了：{len(tests)}"
