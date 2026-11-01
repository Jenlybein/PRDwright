"""管理端契约：账号与额度、邀请码、审计、日志、用量统计、任务监控。

从 `backend.api.schemas` 拆出（#7）；对外引用由包的 `__init__` 再导出。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ============================================================================
# 管理端（admin）：账号、邀请码、审计、日志、统计与任务监控
# ============================================================================


class AdminMeResponse(BaseModel):
    """管理员身份自检的返回。

    刻意**只回自己**的信息：它要回答的是「我有没有进后台的资格」，
    而不是一份用户名单。
    """

    id: int = Field(description="用户 ID")
    username: str = Field(description="用户名")
    is_admin: bool = Field(description="是否管理员；本接口恒为 true，保留字段以便前端统一处理")


class AdminStatsResponse(BaseModel):
    """平台总量。一次请求拿全，避免前端为了凑一个总数连打好几个接口。"""

    user_count: int = Field(description="注册用户数")
    admin_count: int = Field(description="管理员人数")
    task_count: int = Field(description="生成任务总数（含失败）")
    completed_count: int = Field(description="成功完成的任务数")
    prompt_tokens: int = Field(description="输入 Token 合计")
    completion_tokens: int = Field(description="输出 Token 合计")


class AdminUserItem(BaseModel):
    """用户列表的一行。

    用量是**聚合出来的**，不是列表页再去翻任务表：后台首屏如果为此把整张任务表
    读进内存，用户一多就先卡死自己。
    """

    id: int = Field(description="用户 ID")
    username: str = Field(description="用户名")
    created_at: str = Field(description="注册时间（UTC ISO 8601）")
    is_active: bool = Field(description="是否启用；停用后无法登录")
    is_admin: bool = Field(description="是否管理员")
    task_count: int = Field(description="生成任务总数")
    prompt_tokens: int = Field(description="输入 Token 合计")
    completion_tokens: int = Field(description="输出 Token 合计")
    # --- 每日额度（v4.5）---
    daily_task_limit: int = Field(description="生效的每日生成次数上限；0 表示不限")
    daily_token_limit: int = Field(description="生效的每日 Token 上限；0 表示不限")
    max_running: int = Field(description="生效的同时运行任务上限；0 表示不限")
    daily_task_source: str = Field(
        description="次数上限来自哪里：`global` 继承全局 / `user` 单独设置 / `off` 显式不限"
    )
    daily_token_source: str = Field(description="Token 上限来源，取值同上")
    max_running_source: str = Field(description="并发上限来源，取值同上")
    usage_tasks_today: int = Field(description="今天已占用的生成次数（不含失败与取消）")
    usage_tokens_today: int = Field(description="今天已消耗的 Token（含失败任务的实际消耗）")


class AdminSetQuotaRequest(BaseModel):
    """设置某个用户的额度覆盖值。

    三个字段的语义一致：`-1` 跟随全局默认，`0` 不限，正整数为具体上限。
    `-1` 之所以要能显式传，是因为「恢复成跟随全局」和「设成不限」是两件事——
    只给一个空值的话，这两种意图在数据上无法区分。
    """

    daily_task_limit: int = Field(
        ge=-1, description="每日生成次数：-1 跟随全局，0 不限，正数为上限"
    )
    daily_token_limit: int = Field(
        ge=-1, description="每日 Token 上限：-1 跟随全局，0 不限，正数为上限"
    )
    max_running: int = Field(
        ge=-1, description="同时运行任务数：-1 跟随全局，0 不限，正数为上限"
    )


class AdminUserListResponse(BaseModel):
    """用户列表。"""

    users: list[AdminUserItem] = Field(description="全部用户，按 ID 升序")


class AdminCreateUserRequest(BaseModel):
    """后台建号。

    约束与注册保持一致（3-32 位用户名、至少 8 位密码）——后台不该是弱密码后门。
    """

    username: str = Field(min_length=3, max_length=32, description="用户名，3-32 个字符")
    password: str = Field(min_length=8, description="密码，至少 8 位")
    is_admin: bool = Field(default=False, description="是否直接任命为管理员")


class AdminUserResponse(BaseModel):
    """单个账号的当前状态。"""

    id: int = Field(description="用户 ID")
    username: str = Field(description="用户名")
    is_active: bool = Field(description="是否启用")
    is_admin: bool = Field(description="是否管理员")


class AdminSetActiveRequest(BaseModel):
    """启停账号。"""

    is_active: bool = Field(description="true 启用 / false 停用")


class AdminSetAdminRequest(BaseModel):
    """授予 / 撤销管理员身份。"""

    is_admin: bool = Field(description="true 授予管理员 / false 撤销管理员")


class AdminResetPasswordRequest(BaseModel):
    """重置密码。"""

    password: str = Field(min_length=8, description="新密码，至少 8 位")


class AdminInviteItem(BaseModel):
    """邀请码列表的一行。"""

    code: str = Field(description="邀请码")
    max_uses: int = Field(description="最多可用次数；0 表示不限")
    used_count: int = Field(description="已使用次数")
    expires_at: str | None = Field(default=None, description="过期时间；null 表示永不过期")
    created_at: str = Field(description="创建时间")
    note: str | None = Field(default=None, description="备注")


class AdminInviteListResponse(BaseModel):
    """邀请码列表。"""

    invites: list[AdminInviteItem] = Field(description="全部邀请码")


class AdminCreateInviteRequest(BaseModel):
    """新建邀请码。"""

    code: str = Field(min_length=4, max_length=32, description="邀请码，4-32 个字符")
    max_uses: int = Field(default=0, description="最多可用次数；0 表示不限")
    expires_at: str | None = Field(default=None, description="过期时间，ISO 8601；留空表示永不过期")
    note: str = Field(default="", description="备注，如「给 XX 公司」")


class AdminInviteResponse(BaseModel):
    """新建邀请码的结果。"""

    code: str = Field(description="已创建的邀请码")


class AdminOkResponse(BaseModel):
    """无返回内容的操作（删除、重置密码）的统一响应。"""

    ok: bool = Field(default=True, description="是否成功")


class AdminRevokeSessionsResponse(BaseModel):
    """强制下线的结果。

    刻意回**条数**而不是 `ok`：单点登录下每人至多一条会话，`revoked=0` 表示
    「他本来就没登录」。若只回 ok，管理员无法区分「踢掉了」与「本来就不在」。
    """

    revoked: int = Field(description="实际吊销的会话条数；0 表示该用户当时并未登录")


class AdminDeletionPreview(BaseModel):
    """删除账号前的「会删掉什么 / 会留下什么」。

    存在的理由是删除不可逆且原先只问一句「确认删除」——管理员并不知道自己点掉的
    是 1 份方案还是 30 份。`retained_*` 两项把「按设计会保留」的数据提前讲明白，
    免得事后看到日志里还有这个人，以为没删干净。
    """

    user_id: int = Field(description="目标账号 id")
    username: str = Field(description="目标账号用户名")
    solutions: int = Field(description="将被删除的方案数")
    documents: int = Field(description="将被删除的文档数（三份产物各算一份）")
    document_versions: int = Field(description="将被删除的正文版本数（含历史版本）")
    chat_turns: int = Field(description="将被删除的对话轮次数")
    jobs: int = Field(description="将被删除的生成任务数（平台历史统计会随之减少）")
    running_jobs: int = Field(description="其中正在运行、删除前会先被取消的任务数")
    sessions: int = Field(description="将被吊销的登录会话数")
    prompt_tokens: int = Field(description="这些任务累计消耗的输入 Token")
    completion_tokens: int = Field(description="这些任务累计消耗的输出 Token")
    retained_audit: int = Field(description="会保留的操作审计条数（他作为操作者的记录）")
    retained_logs: int = Field(description="会保留的应用日志条数（运维记录，不是用户数据）")


class AdminAuditItem(BaseModel):
    """一条管理员操作记录。"""

    id: int = Field(description="记录 ID")
    actor_id: int | None = Field(default=None, description="操作者 ID；账号已删时为 null")
    actor_name: str = Field(description="操作者用户名（冗余保存，账号删除后仍可见）")
    action: str = Field(description="动作，如 user.create / invite.delete")
    target: str = Field(description="被操作对象")
    detail: str = Field(description="变更内容；不含密码等敏感值")
    request_id: str | None = Field(default=None, description="请求 ID，可与日志对上")
    ts: str = Field(description="发生时间（UTC ISO 8601）")


class AdminAuditListResponse(BaseModel):
    """管理员操作记录。"""

    items: list[AdminAuditItem] = Field(description="最近的操作，倒序")


class AdminLogItem(BaseModel):
    """一条应用日志（`app_logs` 的一行）。

    `extra` 是后端**已经解析好的**对象，不是原始字符串：前端不该为了看一个
    `job_id` 再写一遍 JSON 解析与容错。解析失败时后端给 `{}`，不会让整个列表失败。
    """

    id: int = Field(description="日志 ID（自增，用于前端列表的 key）")
    ts: str = Field(description="发生时间（UTC ISO 8601，毫秒精度）")
    level: str = Field(description="级别：DEBUG / INFO / WARNING / ERROR / CRITICAL")
    logger: str = Field(description="来源 logger 名")
    msg: str = Field(description="日志正文")
    request_id: str | None = Field(default=None, description="请求 ID；启动日志等无请求来源时为 null")
    user_id: int | None = Field(default=None, description="归属用户；无归属时为 null")
    event: str | None = Field(default=None, description="事件名，如 job.started / llm.call_retry")
    extra: dict[str, Any] = Field(default_factory=dict, description="业务字段（job_id / 耗时 / token 等）")
    exc: str | None = Field(default=None, description="异常堆栈（仅 ERROR 类日志有）")


class AdminLogListResponse(BaseModel):
    """日志检索结果（一页）。

    `total` 是**命中总数**而不是本页条数：分页器要靠它算页数，前端自己推算会不准。
    """

    items: list[AdminLogItem] = Field(description="当前页，按时间倒序")
    total: int = Field(description="命中总数")
    page: int = Field(description="当前页码，从 1 开始")
    page_size: int = Field(description="每页条数")


class AdminDailyStat(BaseModel):
    """按天的用量聚合（一天一行）。

    **没有任务的那天也会出现（各项为 0）**：折线图若跳过空白日，会把「那天没人用」
    画成一条平滑的斜线，看起来像用量在缓慢变化——而真相是那天一个都没有。
    """

    day: str = Field(description="本地日期，形如 2026-10-22")
    task_count: int = Field(description="当天创建的任务数（含运行中）")
    completed_count: int = Field(description="成功完成数")
    failed_count: int = Field(description="**真失败**数，已排除主动取消与被顶替")
    cancelled_count: int = Field(description="主动取消 / 被新任务顶替的数量")
    running_count: int = Field(description="仍在该天创建、尚未收尾的任务数")
    prompt_tokens: int = Field(description="输入 Token 合计")
    completion_tokens: int = Field(description="输出 Token 合计")
    failure_rate: float | None = Field(
        default=None,
        description="失败率（已收尾任务中真失败的比例）。分母为 0 时为 null——"
        "「那天没有任务」与「那天 0% 失败」是两回事。",
    )


class AdminDailyStatsResponse(BaseModel):
    """按日趋势 + 这段区间的汇总。

    `summary` 由后端算，**不让前端把每天的数字再加一遍**：失败率若两处各算一套，
    改口径时必然漏一处，页面上日线与汇总行就会互相矛盾。
    """

    items: list[AdminDailyStat] = Field(description="按日期升序的每一天")
    summary: AdminDailyStat | None = Field(
        default=None,
        description="区间汇总（字段与单日一致，`day` 为空串）。失败率为加权值而非每日平均。",
    )
    days: int = Field(description="实际返回的天数")


class AdminTopUserItem(BaseModel):
    """用量排行的一行。

    与用户列表**共用同一段聚合表达式**：「这个用户用了多少」只能有一个算法，
    否则两个页面给出两个数，而没人知道该信哪个。
    """

    id: int = Field(description="用户 ID")
    username: str = Field(description="用户名")
    is_active: bool = Field(description="是否启用")
    task_count: int = Field(description="生成任务总数")
    prompt_tokens: int = Field(description="输入 Token 合计")
    completion_tokens: int = Field(description="输出 Token 合计")


class AdminTopUsersResponse(BaseModel):
    """用量排行。只含真的生成过任务的人。"""

    users: list[AdminTopUserItem] = Field(description="按任务数、再按 Token 降序")


# ----------------------------------------------------------------------------
# 任务监控（v4.5 T5-1）
# ----------------------------------------------------------------------------


class AdminJobItem(BaseModel):
    """任务监控列表的一行。

    `duration_seconds` / `is_stuck` / `stuck_minutes` 都由**服务端算好**：
    浏览器与服务器时钟有偏差，前端自己算会让「列表说卡死」与「点了还能取消」
    互相矛盾。
    """

    id: int = Field(description="任务 ID")
    owner_id: int = Field(description="归属用户 ID")
    owner_name: str = Field(description="归属用户名")
    kind: str = Field(description="任务类型：prd / api_docs / prompts / optimize / chat")
    status: str = Field(description="running / completed / failed")
    phase: str = Field(description="当前阶段（如 writing / reviewing）")
    created_at: str = Field(description="创建时间（UTC ISO 8601）")
    updated_at: str = Field(description="最后更新时间（UTC ISO 8601）")
    duration_seconds: int = Field(description="已运行秒数；running 为「至今」，其余为收尾耗时")
    is_stuck: bool = Field(description="是否判定为卡死（running 且超过阈值未更新）")
    stuck_minutes: int = Field(description="已停更分钟数；仅 is_stuck 为 true 时有意义")
    prompt_tokens: int = Field(description="输入 Token")
    completion_tokens: int = Field(description="输出 Token")
    error_code: str = Field(description="终态错误码（如 CANCELLED / LLM_UPSTREAM_ERROR）")
    error_message: str = Field(description="终态错误说明")


class AdminJobListResponse(BaseModel):
    """任务监控列表。"""

    items: list[AdminJobItem] = Field(description="当前返回的任务")
    total: int = Field(description="同口径命中总数（不只是本页条数）")


class AdminCancelJobResponse(BaseModel):
    """取消单条任务的结果。"""

    cancelled: bool = Field(
        description="是否确实取消了一个仍在运行的任务；false 表示它在点击前已结束"
    )


class AdminCancelStuckResponse(BaseModel):
    """批量取消卡死任务的结果。"""

    cancelled: int = Field(description="实际取消的任务条数")
