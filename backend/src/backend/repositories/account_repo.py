"""账号级数据的整体读取：数据导出（T5-4）与「彻底删除」预览。

为什么单独一个模块
==================
导出与删除预览要回答的都是「这个账号到底有多少东西」，需要**跨表聚合**
（方案 / 文档 / 版本 / 对话 / 任务 / 会话），而既有仓储都是「一张表一件事」。
把它们分散到四个仓储里各加一个 `count_for_user`，调用方就得自己拼结果、
自己记住哪张表算漏了——而漏算在这里的后果是「导出不全」或「预览少报」，
两者都不会报错，只是安静地少了一点。

边界：**管理面、跨 owner，安全性由路由层的 `require_admin` 保证**。
与 `generation_job_repo` 的 `admin_*` 系列同一纪律（ADR-046）：
用户面绝不可调用本模块，新增方法也必须留在这一段注释之下。

## 形态（v6 / T16.6）

绑定 `Session` 的实例方法；**本类不 commit / rollback / close**。
`counts()` 返回聚合字典（没有对应的行），`export_jobs()` 返回 `GenerationJob` 模型对象。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlmodel import Session

from backend.core.models import (
    AdminAuditLog,
    AppLog,
    AuthSession,
    ChatTurn,
    Document,
    DocumentVersion,
    GenerationJob,
    Solution,
)


def _count_where(model: type, *conditions) -> Any:
    """`SELECT COUNT(*) FROM <model> WHERE ...` 的标量子查询。"""
    return (
        select(func.count()).select_from(model).where(*conditions).scalar_subquery()
    )


class AccountDataRepository:
    """管理面：一个账号的全部业务数据规模与导出所需的任务明细（绑定一个 `Session`）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def counts(self, user_id: int) -> dict[str, int]:
        """返回该账号的各类数据条数与 Token 合计。

        键名与导出/预览的字段一一对应，避免调用方再翻译一次。

        全部用**标量子查询**而不是多表 JOIN：JOIN 之后再 `COUNT(*)` 会因为笛卡尔积
        把计数互相放大（`solutions × documents`），那是这类统计最经典的错法，
        而且错得很安静——数字看起来像真的。
        """
        # 文档与版本按「方案归属」过滤，所以要 JOIN 回 solutions
        documents_of_user = (
            select(func.count())
            .select_from(Document)
            .join(Solution, Solution.id == Document.solution_id)
            .where(Solution.user_id == user_id)
            .scalar_subquery()
        )
        versions_of_user = (
            select(func.count())
            .select_from(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Solution, Solution.id == Document.solution_id)
            .where(Solution.user_id == user_id)
            .scalar_subquery()
        )

        statement = select(
            _count_where(Solution, Solution.user_id == user_id).label("solutions"),
            documents_of_user.label("documents"),
            versions_of_user.label("document_versions"),
            _count_where(ChatTurn, ChatTurn.user_id == user_id).label("chat_turns"),
            _count_where(GenerationJob, GenerationJob.owner_id == user_id).label("jobs"),
            _count_where(
                GenerationJob,
                GenerationJob.owner_id == user_id,
                GenerationJob.status == "running",
            ).label("running_jobs"),
            _count_where(AuthSession, AuthSession.user_id == user_id).label("sessions"),
            func.coalesce(
                select(func.sum(GenerationJob.prompt_tokens))
                .where(GenerationJob.owner_id == user_id)
                .scalar_subquery(),
                0,
            ).label("prompt_tokens"),
            func.coalesce(
                select(func.sum(GenerationJob.completion_tokens))
                .where(GenerationJob.owner_id == user_id)
                .scalar_subquery(),
                0,
            ).label("completion_tokens"),
            # 下面两项是「删除**之后仍然留着**」的数据量：审计是问责底线、日志是运维记录，
            # 都不随账号消失（也没有外键）。删号前把它们列出来，管理员才知道留下了什么。
            _count_where(AdminAuditLog, AdminAuditLog.actor_id == user_id).label(
                "retained_audit"
            ),
            _count_where(AppLog, AppLog.user_id == user_id).label("retained_logs"),
        )
        row = self.session.execute(statement).one()
        return {key: int(value or 0) for key, value in row._mapping.items()}

    def export_jobs(self, user_id: int) -> list[GenerationJob]:
        """导出用的任务明细（写进 CSV）。

        **刻意只取「事后来看这一次生成发生了什么」这个用法所需的字段**——
        调用方（`export_service`）用 `DictWriter(extrasaction="ignore")` 只挑要的列，
        所以这里返回整行模型不会把 `draft_content` 漏进 CSV：
        `draft_content` 是正文缓冲，单个任务就可能几十 KB，导出它既撑大文件
        又泄漏「未定稿的中间态」。
        """
        return list(
            self.session.execute(
                select(GenerationJob)
                .where(GenerationJob.owner_id == user_id)
                .order_by(GenerationJob.created_at, GenerationJob.id)
            )
            .scalars()
            .all()
        )
