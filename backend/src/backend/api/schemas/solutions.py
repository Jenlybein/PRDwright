"""方案（solutions）：整份会话快照的持久化与列表 / 详情。

从 `backend.api.schemas` 拆出（#7）；对外引用由包的 `__init__` 再导出。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ============================================================================
# 方案（solutions）：整份会话快照的持久化
# ============================================================================


class SolutionStatePayload(BaseModel):
    """方案的状态（相位 + 草稿）。

    它们曾经和聊天记录一起塞在一列 `data` 的 JSON 里。拆出来之后各自是标量列：
    整份覆盖仍然原子，但不再和「只增不减的记录」挤在同一格里——
    后者一旦被整份覆盖，两个写者就会互相把对方的记录盖掉。

    `summary` 是**唯一留在 JSON 里**的一项：它是张问卷，加一个问题就加一个字段，
    拆成列等于每次改问卷都要写迁移。演进快的结构放 JSON，演进慢的建列。

    ⚠️ 这里**不含 `messages`**：聊天记录由服务端写入 `chat_turns`，前端只读
    （`GET /api/solutions/{id}/turns`）。让前端再提交一份，等于又回到「一个内容
    两个真源、没有裁决者」。
    """

    view_state: str = Field(default="", description="当前所处阶段")
    entry_mode: str = Field(default="", description="入口模式；空串表示未选择")
    chat_tab: str = Field(default="", description="对话页停留的线程；空串表示未进入对话")
    summary: dict[str, Any] | None = Field(
        default=None, description="结构化需求摘要；未提交过为 null"
    )
    quick_product_name: str = Field(default="", description="快捷入口：产品名称")
    quick_requirements: str = Field(default="", description="快捷入口：一段话需求")
    draft_prd_text: str = Field(default="", description="「我有 PRD」里贴入的草稿正文")
    paste_prd_text: str = Field(default="", description="「PRD + 接口文档」里的 PRD 草稿")
    paste_api_docs_text: str = Field(
        default="", description="「PRD + 接口文档」里的接口文档草稿"
    )
    active_job_id: int | None = Field(default=None, description="进行中的任务 id；无任务为 null")
    active_job_kind: str = Field(default="", description="进行中任务的类型")
    active_job_doc_kind: str = Field(default="", description="进行中任务写的是哪一份产物")


class SolutionSaveRequest(BaseModel):
    """新建 / 更新方案的请求体。

    `title` / `mode` / `stage` 是**冗余摘要**：列表接口只读这三列，不必把状态列
    与草稿取出来。冗余值由前端计算——「产品名该从 summary 还是 quickInput 取」
    只有前端清楚。三者的长度上限刻意放宽，超长由服务层截断：它们只是展示用的
    冗余，为此把整次保存判为 422 是本末倒置（用户会看到「保存失败」，数据其实没事）。

    `content_changed` 也是前端算的：它的 `contentTick` 由「摘要 / 草稿 / 对话」
    的变更驱动，「翻了一页」不计入，而这件事决定要不要推进 `content_updated_at`——
    列表的时间与排序都看它。
    """

    title: str = Field(default="", max_length=2000, description="方案标题，列表展示用")
    mode: str = Field(default="", max_length=200, description="入口模式，列表展示用")
    stage: str = Field(default="", max_length=200, description="当前阶段，列表展示进度用")
    state: SolutionStatePayload = Field(
        default_factory=SolutionStatePayload, description="相位与草稿（不含聊天记录）"
    )
    content_changed: bool = Field(
        default=False,
        description=(
            "本次改动是否动了作品内容（而非只是切换阶段 / 翻看）。"
            "为真才推进 `content_updated_at`；缺省为假，即「这次保存不改变内容时间」。"
        ),
    )


class SolutionSummary(BaseModel):
    """方案摘要，用于左侧列表。刻意**不含** `data`。"""

    id: int = Field(description="方案 ID")
    title: str = Field(description="方案标题；未取到产品名时由前端给出默认标题")
    mode: str = Field(default="", description="入口模式")
    stage: str = Field(default="", description="当前阶段")
    created_at: str = Field(description="创建时间（UTC ISO 8601）")
    updated_at: str = Field(
        description="最近一次写库时间（UTC ISO 8601）。注意它**含**「只是翻看」这类保存，"
        "要回答「作品何时真的变了」请用 `content_updated_at`"
    )
    content_updated_at: str = Field(
        description=(
            "最后一次**内容**变更时间（UTC ISO 8601）：需求摘要 / 对话 / 粘贴材料变了，"
            "或三份产物被生成、优化、手改、恢复时推进。列表的时间显示与排序都用它"
        )
    )


class SolutionDetail(SolutionSummary):
    """方案详情：摘要 + 状态（相位与草稿）。

    **不含聊天记录**：轮次单独走 `GET /api/solutions/{id}/turns`，按线程取。
    两者放在一个响应里会让「打开方案」变成一次大请求，也会让「只轮询轮次」
    顺带把草稿一起拖着走。
    """

    state: SolutionStatePayload = Field(
        default_factory=SolutionStatePayload, description="相位与草稿（不含聊天记录）"
    )


# ----------------------------------------------------------------------------
# 取消任务：与方案的「进行中任务」一起用，放在方案域里
# ----------------------------------------------------------------------------


class JobCancelResponse(BaseModel):
    """取消任务的结果。

    `cancelled` 与 HTTP 200 分开表达：任务可能刚刚自然结束，此时「取消」什么也没做，
    但请求本身是合法的——用 409 报错会让前端把它当成失败，而实际上什么都不用做。
    """

    ok: bool = Field(default=True, description="请求已处理")
    cancelled: bool = Field(description="是否确实取消了一个仍在运行的任务")


class SolutionListResponse(BaseModel):
    """方案列表响应。

    包一层对象而不是直接返回数组：将来要加分页或总数时不必改响应结构，
    前端也不会因为「根是数组」而在扩展时被迫重写解析。
    """

    solutions: list[SolutionSummary] = Field(
        default_factory=list, description="当前用户的方案摘要列表"
    )


class SolutionDeleteResponse(BaseModel):
    """删除方案响应。"""

    ok: bool = Field(default=True, description="是否成功删除")
