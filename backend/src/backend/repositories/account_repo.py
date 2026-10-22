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
"""

from __future__ import annotations

from typing import Any

from backend.core.db import connect

# 一次拿到一个账号的全部计数。全部用标量子查询而不是多表 JOIN：
# 多表 JOIN 后再 `COUNT(*)` 会因为笛卡尔积把计数互相放大（`solutions × documents`），
# 那是这类统计最经典的错法，而且错得很安静——数字看起来像真的。
_COUNT_SQL = """
SELECT
    (SELECT COUNT(*) FROM solutions         WHERE user_id  = ?) AS solutions,
    (SELECT COUNT(*) FROM documents d
       JOIN solutions s ON s.id = d.solution_id
      WHERE s.user_id = ?)                                      AS documents,
    (SELECT COUNT(*) FROM document_versions v
       JOIN documents d ON d.id = v.document_id
       JOIN solutions s ON s.id = d.solution_id
      WHERE s.user_id = ?)                                      AS document_versions,
    (SELECT COUNT(*) FROM chat_turns        WHERE user_id  = ?) AS chat_turns,
    (SELECT COUNT(*) FROM generation_jobs   WHERE owner_id = ?) AS jobs,
    (SELECT COUNT(*) FROM generation_jobs
      WHERE owner_id = ? AND status = 'running')                AS running_jobs,
    (SELECT COUNT(*) FROM auth_sessions     WHERE user_id  = ?) AS sessions,
    (SELECT COALESCE(SUM(prompt_tokens), 0)     FROM generation_jobs
      WHERE owner_id = ?)                                       AS prompt_tokens,
    (SELECT COALESCE(SUM(completion_tokens), 0) FROM generation_jobs
      WHERE owner_id = ?)                                       AS completion_tokens,
    -- 下面两项是「删除**之后仍然留着**」的数据量：审计是问责底线、日志是运维记录，
    -- 都不随账号消失（也没有外键）。删号前把它们列出来，管理员才知道留下了什么。
    (SELECT COUNT(*) FROM admin_audit_log   WHERE actor_id = ?) AS retained_audit,
    (SELECT COUNT(*) FROM app_logs          WHERE user_id  = ?) AS retained_logs
"""


class AccountDataRepository:
    """管理面：一个账号的全部业务数据规模与导出所需的任务明细。"""

    @staticmethod
    def counts(user_id: int) -> dict[str, int]:
        """返回该账号的各类数据条数与 Token 合计。

        键名与导出/预览的字段一一对应，避免调用方再翻译一次。
        """
        # 11 个占位符全绑同一个 user_id：写成一列参数比数着问号排更不容易错位
        params = [user_id] * 11
        with connect() as conn:
            row = conn.execute(_COUNT_SQL, params).fetchone()
            return {key: int(value or 0) for key, value in dict(row).items()}

    @staticmethod
    def export_jobs(user_id: int) -> list[dict[str, Any]]:
        """导出用的任务明细（写进 CSV）。

        **刻意不 `SELECT *`**：`draft_content` 是正文缓冲，单个任务就可能几十 KB，
        导出整表既撑大文件又泄漏「未定稿的中间态」。这里只取「事后来看这一次生成
        发生了什么」所需的列。
        """
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, kind, doc_kind, chat_stage, status, phase, solution_id, "
                "       created_at, updated_at, prompt_tokens, completion_tokens, "
                "       error_code, error_message "
                "FROM generation_jobs WHERE owner_id = ? ORDER BY created_at ASC, id ASC",
                (user_id,),
            ).fetchall()
            return [dict(row) for row in rows]
