"""分层的静态不变量：services / repositories 不得反向依赖 api。

依赖方向约定为 `api → services → repositories → core`。`services/` 与
`repositories/` 若回头 import `api/`，就形成反向依赖（甚至循环 import）：
业务层被接口层的数据结构绑死，改一处响应模型就得动业务代码。

历史背景：`StreamEvent` / `QuestionsConfig` 曾经定义在 `api/schemas.py`，
被 5 个 service 反向 import。本次把这三个契约下沉到 `core/contracts.py`，
`api/schemas.py` 只做 re-export。本文件是**防回退守卫**：跑一次 pytest
就能发现有人又把 `from backend.api` 写了回去。

扫描规则简单到不会被绕过——整份源码文本里出现 `from backend.api` 或
`import backend.api` 即判失败（注释里提到也算）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import BaseModel

# 仓库根 → 后端源码目录
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_SRC = _REPO_ROOT / "backend" / "src" / "backend"

# 禁止反向依赖接口层的两个目录
_GUARDED_DIRS = ("services", "repositories")

# 匹配 `from backend.api...` 或 `import backend.api...` 两种写法
_FORBIDDEN_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+backend\.api(?:\.[\w.]+)?\s+import\b|import\s+backend\.api(?:\.[\w.]+)?\b)",
    re.MULTILINE,
)

# 具体供应商 SDK。业务代码只认 `llm_factory.get_llm()` 与统一的 `StreamingChatModel`
# 协议，绝不直接 import 任何一家的 SDK。
#
# 六个协议各自的 SDK 都要扫到：少扫一个，那一家就可能在任意 service 里被直连，
# 而「换供应商只改工厂」的承诺就破了。
_VENDOR_SDK_RE = re.compile(
    r"^\s*(?:from|import)\s+"
    r"(arkruntime|langchain_openai|langchain_anthropic|"
    r"langchain_google_genai|langchain_nvidia_ai_endpoints|langchain_huggingface)\b",
    re.MULTILINE,
)

# 唯一允许直连供应商 SDK 的文件。
#
# ⚠️ 这里从两个文件（`llm_factory.py` + `ark_llm.py`）收敛成一个：方舟适配器已并入
# `llm_factory.py`。白名单每多一个文件，就多一处「新加适配器时忘了登记」的机会；
# 收敛之后规则只剩一句：**只有工厂能碰供应商 SDK**。
_VENDOR_SDK_ALLOWED = frozenset({"llm_factory.py"})


def _scan_for_backward_imports(directory: Path) -> list[str]:
    """扫描目录下所有 .py 文件，返回「反向 import api」的违规行。

    Args:
        directory: 待扫描的目录（如 `services/`）。

    Returns:
        形如 ``文件相对路径:行号 → 源码行`` 的违规描述列表；无违规则为空列表。
    """
    offenders: list[str] = []
    if not directory.exists():
        return offenders
    for path in sorted(directory.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in _FORBIDDEN_IMPORT_RE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            line = text.splitlines()[line_no - 1].strip()
            offenders.append(f"{path.relative_to(_BACKEND_SRC)}:{line_no} → {line}")
    return offenders


def test_forbidden_import_detector_is_not_vacuous():
    """自检：扫描规则必须真能抓到反向依赖的写法。

    一条「永远不报错」的守卫等于没有守卫，所以先用一段已知的坏代码
    证明正则睁着眼——避免正则写错却静默失明。
    """
    assert _FORBIDDEN_IMPORT_RE.search("from backend.api.schemas import StreamEvent")
    assert _FORBIDDEN_IMPORT_RE.search("from backend.api import schemas")
    assert _FORBIDDEN_IMPORT_RE.search("import backend.api.schemas")
    # 正向依赖（core / services）不该被误报
    assert not _FORBIDDEN_IMPORT_RE.search("from backend.core.contracts import StreamEvent")
    assert not _FORBIDDEN_IMPORT_RE.search("from backend.services import document_service")


@pytest.mark.parametrize("layer", _GUARDED_DIRS)
def test_layer_does_not_import_api(layer):
    """`services/` 与 `repositories/` 不得 import `api/`。

    契约已下沉到 `core/contracts.py`：需要 `StreamEvent` / `QuestionsConfig`
    时从 `core` 取，绝不回头 import 接口层。
    """
    offenders = _scan_for_backward_imports(_BACKEND_SRC / layer)

    assert not offenders, (
        f"`{layer}/` 出现对 `api/` 的反向依赖（契约应放在 core/contracts.py）："
        + "；".join(offenders)
    )


def test_vendor_sdk_detector_is_not_vacuous():
    """自检：供应商 SDK 扫描规则必须真能命中直连写法。"""
    assert _VENDOR_SDK_RE.search("from arkruntime import AsyncArk")
    assert _VENDOR_SDK_RE.search("import langchain_openai")
    assert _VENDOR_SDK_RE.search("from langchain_anthropic import ChatAnthropic")
    # 新加的三个协议包也要真的被扫到（少扫一个，那家就能被直连）
    assert _VENDOR_SDK_RE.search("from langchain_google_genai import ChatGoogleGenerativeAI")
    assert _VENDOR_SDK_RE.search(
        "from langchain_nvidia_ai_endpoints import ChatNVIDIA"
    )
    assert _VENDOR_SDK_RE.search("from langchain_huggingface import ChatHuggingFace")
    # 抽象层与自家的工厂不该被误报
    assert not _VENDOR_SDK_RE.search("from langchain_core.messages import AIMessageChunk")
    assert not _VENDOR_SDK_RE.search(
        "from backend.services.llm_factory import VolcanoArkChatModel"
    )


def test_only_adapters_import_vendor_sdk():
    """业务层不得直接 import 供应商 SDK（方舟 / OpenAI / Anthropic）。

    换供应商时要改的只有工厂（`llm_factory`，六家适配器都在里面）。
    若某个 service 直接 import `arkruntime` / `langchain_openai`，换供应商就成了
    全仓大改；用量映射这类适配逻辑也会被复制到各处，迟早走形。
    """
    offenders: list[str] = []
    for path in sorted((_BACKEND_SRC / "services").glob("*.py")):
        if path.name in _VENDOR_SDK_ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _VENDOR_SDK_RE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.name}:{line_no}")

    assert offenders == [], (
        "业务层出现供应商 SDK 直连（应经 `llm_factory.get_llm()`）：" + "；".join(offenders)
    )

# ---------------------------------------------------------------------------
# API 契约模块（#7）：按领域拆包，对外导出面逐字冻结
# ---------------------------------------------------------------------------
#
# `backend.api.schemas` 曾是 1321 行的单文件（重写清单的 W3）。按领域拆成
# 一个包之后，新增契约落进对应的领域模块，不必再在一千三百多行里找位置。
#
# 但拆的是**模块边界**，不是契约本身：下面这张表是拆分前 dump 出来的对外形状。
# 「顺手改个字段」「挪模型时漏了一个」都会在这里转红；要改契约，就显式改这张表。

_CONTRACT_DIR = _BACKEND_SRC / "api" / "schemas"

# 单文件重写阈值。
_CONTRACT_LINE_LIMIT = 1200

# 领域模块清单。新增一个领域时在这里登记，并在包的 `__init__.py` 里加一行再导出。
_CONTRACT_MODULES = (
    "admin",
    "admin_config",
    "auth",
    "conversation",
    "documents",
    "jobs",
    "solutions",
    "streams",
)

# 拆分后 `backend.api.schemas` 必须暴露的模型名（86 个，含 5 个从 `core.contracts`
# 再导出的共享契约）。值是各模型的字段集合。
_EXPECTED_SCHEMAS_FIELDS = {
    "AdminActiveProvider": {"base_url", "context_window", "default_model", "id", "max_output", "max_tokens", "name", "protocol", "protocol_label", "thinking_enabled"},
    "AdminAuditItem": {"action", "actor_id", "actor_name", "detail", "id", "request_id", "target", "ts"},
    "AdminAuditListResponse": {"items"},
    "AdminCancelJobResponse": {"cancelled"},
    "AdminCancelStuckResponse": {"cancelled"},
    "AdminConfigGroup": {"help", "items", "key", "label"},
    "AdminConfigItem": {"danger", "default", "help", "key", "label", "max", "min", "min_exclusive", "options", "source", "type", "value"},
    "AdminConfigResponse": {"active_provider", "active_provider_id", "config_source", "groups", "missing_key", "revision"},
    "AdminCreateInviteRequest": {"code", "expires_at", "max_uses", "note"},
    "AdminCreateProviderRequest": {"api_key", "base_url", "context_window", "default_model", "extra", "max_output", "max_tokens", "name", "protocol", "sort_order", "thinking_enabled"},
    "AdminCreateUserRequest": {"is_admin", "password", "username"},
    "AdminDailyStat": {"cancelled_count", "completed_count", "completion_tokens", "day", "failed_count", "failure_rate", "prompt_tokens", "running_count", "task_count"},
    "AdminDailyStatsResponse": {"days", "items", "summary"},
    "AdminDeletionPreview": {"chat_turns", "completion_tokens", "document_versions", "documents", "jobs", "prompt_tokens", "retained_audit", "retained_logs", "running_jobs", "sessions", "solutions", "user_id", "username"},
    "AdminInviteItem": {"code", "created_at", "expires_at", "max_uses", "note", "used_count"},
    "AdminInviteListResponse": {"invites"},
    "AdminInviteResponse": {"code"},
    "AdminJobItem": {"completion_tokens", "created_at", "duration_seconds", "error_code", "error_message", "id", "is_stuck", "kind", "owner_id", "owner_name", "phase", "prompt_tokens", "status", "stuck_minutes", "updated_at"},
    "AdminJobListResponse": {"items", "total"},
    "AdminLogItem": {"event", "exc", "extra", "id", "level", "logger", "msg", "request_id", "ts", "user_id"},
    "AdminLogListResponse": {"items", "page", "page_size", "total"},
    "AdminMeResponse": {"id", "is_admin", "username"},
    "AdminOkResponse": {"ok"},
    "AdminProtocolOption": {"label", "value"},
    "AdminProviderItem": {"active", "base_url", "context_window", "default_model", "enabled", "extra", "id", "key_present", "max_output", "max_tokens", "name", "protocol", "protocol_label", "sort_order", "thinking_enabled"},
    "AdminProviderKeyResponse": {"api_key"},
    "AdminProviderListResponse": {"active_provider_id", "presets", "protocols", "providers", "warning"},
    "AdminProviderPreset": {"base_url", "default_model", "key", "name", "protocol"},
    "AdminProviderTestResponse": {"detail", "latency_ms", "ok"},
    "AdminResetConfigRequest": {"keys"},
    "AdminResetPasswordRequest": {"password"},
    "AdminRevokeSessionsResponse": {"revoked"},
    "AdminSetActiveRequest": {"is_active"},
    "AdminSetAdminRequest": {"is_admin"},
    "AdminSetQuotaRequest": {"daily_task_limit", "daily_token_limit", "max_running"},
    "AdminStatsResponse": {"admin_count", "completed_count", "completion_tokens", "prompt_tokens", "task_count", "user_count"},
    "AdminTopUserItem": {"completion_tokens", "id", "is_active", "prompt_tokens", "task_count", "username"},
    "AdminTopUsersResponse": {"users"},
    "AdminUpdateConfigRequest": {"updates"},
    "AdminUpdateProviderRequest": {"api_key", "base_url", "clear_key", "context_window", "default_model", "enabled", "extra", "max_output", "max_tokens", "name", "protocol", "sort_order", "thinking_enabled"},
    "AdminUserItem": {"completion_tokens", "created_at", "daily_task_limit", "daily_task_source", "daily_token_limit", "daily_token_source", "id", "is_active", "is_admin", "max_running", "max_running_source", "prompt_tokens", "task_count", "usage_tasks_today", "usage_tokens_today", "username"},
    "AdminUserListResponse": {"users"},
    "AdminUserResponse": {"id", "is_active", "is_admin", "username"},
    "AuthUserResponse": {"id", "username"},
    "ChatTurn": {"answers", "asks", "content", "created_at", "id", "interrupted", "role", "stage", "tool_calls"},
    "ChatTurnListResponse": {"turns"},
    "ClarificationAnswerItem": {"ask_id", "labels", "question", "texts"},
    "ConversationContinueRequest": {"content", "conversation_id", "history", "profile_key", "tool_result"},
    "ConversationCreateRequest": {"answers", "profile_key", "title"},
    "DocumentCheckpointRequest": {"content", "note"},
    "DocumentItem": {"content", "derived_from", "kind", "last_run_summary", "pending_items", "review", "stale", "updated_at"},
    "DocumentListResponse": {"documents"},
    "DocumentRestoreRequest": {"version_id"},
    "DocumentSaveRequest": {"content"},
    "DocumentVersionDeleteResponse": {"ok", "version_id"},
    "DocumentVersionDetail": {"content", "created_at", "is_current", "last_run_summary", "note", "pending_items", "review", "source", "updated_at", "version_id", "version_no"},
    "DocumentVersionItem": {"content_length", "content_sha", "created_at", "is_current", "note", "source", "version_id", "version_no"},
    "DocumentVersionListResponse": {"versions"},
    "GenerateApiDocsStreamRequest": {"prd_content"},
    "GeneratePrdFromSummaryStreamRequest": {"history", "product_name", "requirements", "summary"},
    "GeneratePrdStreamRequest": {"history", "requirements"},
    "GeneratePromptsStreamRequest": {"prd_content"},
    "JobCancelResponse": {"cancelled", "ok"},
    "JobCreateRequest": {"api_docs_content", "chat_mode", "content", "doc_kind", "document", "form_data", "history", "instruction", "kind", "pending_items", "prd_content", "product_name", "profile_key", "requirements", "review_enabled", "solution_id", "stage", "summary", "tool_result"},
    "JobCreated": {"job_id", "status"},
    "JobListResponse": {"jobs"},
    "JobStatus": {"chat_stage", "created_at", "doc_kind", "error_code", "error_message", "id", "kind", "partial", "phase", "solution_id", "status", "updated_at", "usage"},
    "LoginRequest": {"password", "username"},
    "LogoutResponse": {"ok"},
    "MessageItem": {"content", "role", "tool_call_id", "tool_calls", "tool_result"},
    "OptimizeDocumentStreamRequest": {"document", "instruction"},
    "QuestionConfig": {"label", "name", "options", "required", "type"},
    "QuestionsConfig": {"advanced_questions", "base_questions"},
    "RegisterRequest": {"invite_code", "password", "username"},
    "RunSummary": {"completion_tokens", "duration_ms", "failed_calls", "failed_sections", "injected_skills", "kind", "model", "partial", "prompt_tokens", "protocol", "provider_config_id", "provider_name", "request_id", "status", "steps", "total_calls", "warnings"},
    "SolutionDeleteResponse": {"ok"},
    "SolutionDetail": {"content_updated_at", "created_at", "id", "mode", "stage", "state", "title", "updated_at"},
    "SolutionListResponse": {"solutions"},
    "SolutionSaveRequest": {"content_changed", "mode", "stage", "state", "title"},
    "SolutionStatePayload": {"active_job_doc_kind", "active_job_id", "active_job_kind", "chat_tab", "draft_prd_text", "entry_mode", "paste_api_docs_text", "paste_prd_text", "quick_product_name", "quick_requirements", "summary", "view_state"},
    "SolutionSummary": {"content_updated_at", "created_at", "id", "mode", "stage", "title", "updated_at"},
    "StepUsage": {"calls", "completion_tokens", "duration_ms", "failed_calls", "prompt_tokens", "step"},
    "StreamEvent": {"code", "content", "data", "type"},
    "SummarySyncRequest": {"history", "summary"},
    "SummarySyncResponse": {"changed", "summary"},
    "ToolResultPayload": {"answers", "tool_call_id"},
}

# 两个类型别名不是模型，但同样是调用方可见的契约。
#
# `review`（票 #55）是**有意**新增的一项：重审是独立的任务种类（只审不改），
# 与 `review_fix` 一起构成 ADR 0002 的两个动作。加它必须改这一行——这条守卫的意义
# 正是「契约变了要有人看见」，不是「永远不许变」。
_EXPECTED_JOB_KINDS = (
    "prd",
    "api_docs",
    "prompts",
    "optimize",
    "clarify",
    "auto_clarify",
    "review",
    "chat",
)
_EXPECTED_DOCUMENT_KINDS = ("prd", "api_docs", "prompts")


def test_contract_module_is_split_and_below_line_limit():
    """契约模块按领域拆成包，且每一份都在 1200 行阈值以内。

    双判据：既要有领域模块，也不能出现「拆完之后又长回 1200 行」的单文件。
    """
    assert _CONTRACT_DIR.is_dir(), "backend.api.schemas 应当是按领域拆分的包目录"

    missing = [
        name for name in _CONTRACT_MODULES if not (_CONTRACT_DIR / f"{name}.py").is_file()
    ]
    assert missing == [], f"缺少领域模块：{missing}"

    oversized = [
        f"{path.name}（{len(path.read_text(encoding='utf-8').splitlines())} 行）"
        for path in sorted(_CONTRACT_DIR.glob("*.py"))
        if len(path.read_text(encoding="utf-8").splitlines()) > _CONTRACT_LINE_LIMIT
    ]
    assert oversized == [], f"契约模块仍有超过 {_CONTRACT_LINE_LIMIT} 行的文件：{oversized}"


def test_api_schema_surface_is_frozen():
    """对外可见的模型名与字段集合逐字冻结：拆分只许搬家，不许动契约。

    两种调用形态都由包内 `__init__` 的再导出保证：
    `from backend.api import schemas` + `schemas.XxxModel`，以及
    `from backend.api.schemas import XxxModel`。这里从包入口读一遍：
    任何一个模型消失、改名，或字段集合变化，都会转红。
    """
    import backend.api.schemas as schemas

    actual_models = {
        name
        for name in dir(schemas)
        if not name.startswith("_")
        and isinstance(getattr(schemas, name), type)
        and issubclass(getattr(schemas, name), BaseModel)
        and getattr(schemas, name) is not BaseModel
    }
    assert actual_models == set(_EXPECTED_SCHEMAS_FIELDS), (
        "对外模型名集合变了：多出 "
        f"{sorted(actual_models - set(_EXPECTED_SCHEMAS_FIELDS))}，"
        f"少了 {sorted(set(_EXPECTED_SCHEMAS_FIELDS) - actual_models)}"
    )

    drifted = {
        name: sorted(set(getattr(schemas, name).model_fields) ^ fields)
        for name, fields in _EXPECTED_SCHEMAS_FIELDS.items()
        if set(getattr(schemas, name).model_fields) != fields
    }
    assert drifted == {}, f"对外字段集合变了（模型: 差异字段）：{drifted}"

    assert get_args(schemas.JobKind) == _EXPECTED_JOB_KINDS
    assert get_args(schemas.DocumentKind) == _EXPECTED_DOCUMENT_KINDS
