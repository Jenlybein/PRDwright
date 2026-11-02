"""API 请求 / 响应模型定义（按领域拆分）。

路由层只负责参数校验与序列化，业务数据结构统一在这里声明，
避免请求体 / 响应体的字段散落在各个路由文件里。

## 为什么是包而不是单文件

这里曾是 1321 行的单文件：新增一份契约得在一千三百多行里找落点（#7）。
现在按领域拆成同目录的子模块，而**对外的引用形状一条都没变**：

- `from backend.api import schemas` + `schemas.XxxModel`；
- `from backend.api.schemas import XxxModel`。

两种形态都由本文件的再导出保证。新增模型请落进对应领域模块，再在这里补一行：
这里是契约的唯一出口，也是 `tests/test_layering.py` 里冻结的那张表对照的对象。

## 领域模块

| 模块 | 职责 |
| --- | --- |
| `conversation` | 会话创建 / 接续、工具结果回灌、摘要回填、对话轮次 |
| `streams` | 文档流式生成的请求体（PRD / 接口文档 / 提示词 / 优化） |
| `auth` | 登录、注册、登出与用户信息 |
| `admin` | 管理端：账号与额度、邀请码、审计、日志、统计、任务监控 |
| `admin_config` | 系统配置项与模型供应商配置 |
| `solutions` | 方案快照、方案列表 / 详情、删除与取消任务 |
| `documents` | 文档正文与文档版本（存为新版本 / 查看 / 恢复） |
| `jobs` | 生成任务：创建、状态、列表 |
"""

# 这些契约（StreamEvent / QuestionConfig / QuestionsConfig / StepUsage / RunSummary）
# 已下沉到 `core/contracts.py`，此处 re-export 以保持 API 层引用不变：
# `from backend.api.schemas import StreamEvent / QuestionsConfig` 继续可用。
# `RunSummary` 同时是 `run_summary` 事件与 `generation_jobs.usage` 的**同一份结构**。
from backend.core.contracts import (
    DocumentKind,
    QuestionConfig,
    QuestionsConfig,
    RunSummary,
    StepUsage,
    StreamEvent,
)

from .conversation import (
    ChatTurn,
    ChatTurnListResponse,
    ClarificationAnswerItem,
    ConversationContinueRequest,
    ConversationCreateRequest,
    MessageItem,
    SummarySyncRequest,
    SummarySyncResponse,
    ToolResultPayload,
)

from .streams import (
    GenerateApiDocsStreamRequest,
    GeneratePrdFromSummaryStreamRequest,
    GeneratePrdStreamRequest,
    GeneratePromptsStreamRequest,
    OptimizeDocumentStreamRequest,
)

from .auth import (
    AuthUserResponse,
    LoginRequest,
    LogoutResponse,
    RegisterRequest,
)

from .admin import (
    AdminAuditItem,
    AdminAuditListResponse,
    AdminCancelJobResponse,
    AdminCancelStuckResponse,
    AdminCreateInviteRequest,
    AdminCreateUserRequest,
    AdminDailyStat,
    AdminDailyStatsResponse,
    AdminDeletionPreview,
    AdminInviteItem,
    AdminInviteListResponse,
    AdminInviteResponse,
    AdminJobItem,
    AdminJobListResponse,
    AdminLogItem,
    AdminLogListResponse,
    AdminMeResponse,
    AdminOkResponse,
    AdminResetPasswordRequest,
    AdminRevokeSessionsResponse,
    AdminSetActiveRequest,
    AdminSetAdminRequest,
    AdminSetQuotaRequest,
    AdminStatsResponse,
    AdminTopUserItem,
    AdminTopUsersResponse,
    AdminUserItem,
    AdminUserListResponse,
    AdminUserResponse,
)

from .admin_config import (
    AdminActiveProvider,
    AdminConfigGroup,
    AdminConfigItem,
    AdminConfigResponse,
    AdminCreateProviderRequest,
    AdminProtocolOption,
    AdminProviderItem,
    AdminProviderKeyResponse,
    AdminProviderListResponse,
    AdminProviderPreset,
    AdminProviderTestResponse,
    AdminResetConfigRequest,
    AdminUpdateConfigRequest,
    AdminUpdateProviderRequest,
)

from .solutions import (
    JobCancelResponse,
    SolutionDeleteResponse,
    SolutionDetail,
    SolutionListResponse,
    SolutionSaveRequest,
    SolutionStatePayload,
    SolutionSummary,
)

from .documents import (
    DocumentCheckpointRequest,
    DocumentItem,
    DocumentListResponse,
    DocumentRestoreRequest,
    DocumentSaveRequest,
    DocumentVersionDeleteResponse,
    DocumentVersionDetail,
    DocumentVersionItem,
    DocumentVersionListResponse,
)

from .jobs import (
    JobCreateRequest,
    JobCreated,
    JobKind,
    JobListResponse,
    JobStatus,
)

__all__ = [
    "AdminActiveProvider",
    "AdminAuditItem",
    "AdminAuditListResponse",
    "AdminCancelJobResponse",
    "AdminCancelStuckResponse",
    "AdminConfigGroup",
    "AdminConfigItem",
    "AdminConfigResponse",
    "AdminCreateInviteRequest",
    "AdminCreateProviderRequest",
    "AdminCreateUserRequest",
    "AdminDailyStat",
    "AdminDailyStatsResponse",
    "AdminDeletionPreview",
    "AdminInviteItem",
    "AdminInviteListResponse",
    "AdminInviteResponse",
    "AdminJobItem",
    "AdminJobListResponse",
    "AdminLogItem",
    "AdminLogListResponse",
    "AdminMeResponse",
    "AdminOkResponse",
    "AdminProtocolOption",
    "AdminProviderItem",
    "AdminProviderKeyResponse",
    "AdminProviderListResponse",
    "AdminProviderPreset",
    "AdminProviderTestResponse",
    "AdminResetConfigRequest",
    "AdminResetPasswordRequest",
    "AdminRevokeSessionsResponse",
    "AdminSetActiveRequest",
    "AdminSetAdminRequest",
    "AdminSetQuotaRequest",
    "AdminStatsResponse",
    "AdminTopUserItem",
    "AdminTopUsersResponse",
    "AdminUpdateConfigRequest",
    "AdminUpdateProviderRequest",
    "AdminUserItem",
    "AdminUserListResponse",
    "AdminUserResponse",
    "AuthUserResponse",
    "ChatTurn",
    "ChatTurnListResponse",
    "ClarificationAnswerItem",
    "ConversationContinueRequest",
    "ConversationCreateRequest",
    "DocumentCheckpointRequest",
    "DocumentItem",
    "DocumentKind",
    "DocumentListResponse",
    "DocumentRestoreRequest",
    "DocumentSaveRequest",
    "DocumentVersionDeleteResponse",
    "DocumentVersionDetail",
    "DocumentVersionItem",
    "DocumentVersionListResponse",
    "GenerateApiDocsStreamRequest",
    "GeneratePrdFromSummaryStreamRequest",
    "GeneratePrdStreamRequest",
    "GeneratePromptsStreamRequest",
    "JobCancelResponse",
    "JobCreateRequest",
    "JobCreated",
    "JobKind",
    "JobListResponse",
    "JobStatus",
    "LoginRequest",
    "LogoutResponse",
    "MessageItem",
    "OptimizeDocumentStreamRequest",
    "QuestionConfig",
    "QuestionsConfig",
    "RegisterRequest",
    "RunSummary",
    "SolutionDeleteResponse",
    "SolutionDetail",
    "SolutionListResponse",
    "SolutionSaveRequest",
    "SolutionStatePayload",
    "SolutionSummary",
    "StepUsage",
    "StreamEvent",
    "SummarySyncRequest",
    "SummarySyncResponse",
    "ToolResultPayload",
]
