"""文档（documents）与文档版本（document_versions）：正文与版本链的读写契约。

从 `backend.api.schemas` 拆出（#7）；对外引用由包的 `__init__` 再导出。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from backend.core.contracts import DocumentKind

# ============================================================================
# 文档（documents）：三份产物正文，后端持有
# ============================================================================
# 与 `solutions` 的关系：方案是「一次会话的全量快照」，文档是「某一份产物」。
# 相位（当前阶段、对话记录）留在方案快照里，正文搬到文档表——两者生命周期不同：
# 相位随操作频繁变，正文只在生成或用户手改时变。


class DocumentItem(BaseModel):
    """一份文档的对外结构。

    刻意不给 `id` / `solution_id` / `content_sha`：调用方用「方案 id + 产物类型」
    就能定位一份文档，主键是内部细节；内容指纹是排查用的，不属于对外契约。
    """

    kind: DocumentKind = Field(description="产物类型")
    content: str = Field(
        default="", description="文档正文（Markdown 原文；内部标记块已在后端剥离）"
    )
    pending_items: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "待确认清单：模型产出的结构化元数据，**不由用户编辑**，也不出现在正文里。"
            "旧文档与尚未生成时为空列表。"
        ),
    )
    last_run_summary: dict[str, Any] | None = Field(
        default=None,
        description=(
            "最后一次生成的用量回执。**按不透明对象透传**、不在后端校验字段："
            "迁移自旧快照的回执是前端 camelCase 形状，而新写入的是后端形状，"
            "两者并存；前端已有容错解析器，后端强校验只会把能显示的回执变成 500。"
        ),
    )
    review: dict[str, Any] | None = Field(
        default=None,
        description=(
            "**当前版本**那一条审核结论（一版一条，形状与 SSE `review` 事件的数据一致："
            "kind / verdict / summary / issues / rewritten / failed_sections / reviewed_at / "
            "attempted；`issues` 每条带 `category`，旧结论没有这一格时由前端归「未分类」）。"
            "`None` 表示这一版没审过或审核没跑成——**不等于通过**，界面照实标"
            "「未审核 / 审核失败」。刷新与换会话后它是唯一来源。"
        ),
    )
    review_failed_at: str | None = Field(
        default=None,
        description=(
            "**当前版本**上一次审核试过但没跑成的时间（UTC ISO 8601）；`None` = 没有失败记录。"
            "生成时的审核失败与修复后的重审失败写的是同一列。它是落库事实：失败不写结论"
            "（没有结论 ≠ 通过），但刷新后仍要能显示「审核失败（时间）」，否则界面会退化成"
            "「未审核」。拿到新结论时清空。"
        ),
    )
    version_no: int = Field(
        default=0,
        description=(
            "当前版本序号（从 1 开始）。结论面板用它回答「这条结论是哪一版的」；"
            "0 表示还没有版本（旧数据 / 尚未生成），界面不显示版本号。"
        ),
    )
    updated_at: str = Field(default="", description="最近更新时间（UTC ISO 8601）")
    partial: bool = Field(
        default=False,
        description=(
            "当前版本是否**可用但不完整**（票 #85）：失败收尾把已生成的正文落成新版本、"
            "或有章节落成失败占位时为 true。它只说明「这份正文完整不完整」，"
            "**不**影响下载 / 修复 / 重生成是否可用。旧数据恒为 false。"
        ),
    )
    stale: bool = Field(
        default=False,
        description="这份下游文档是否基于旧版上游（按 `derived_from` 记录的指纹比较）。"
        "只对接口文档 / 提示词有意义；PRD 没有上游，恒为 false。",
    )
    derived_from: dict[str, Any] | None = Field(
        default=None,
        description="生成这份文档时的上游锚点（`prd` / 可选 `api_docs` 的版本号 + 内容指纹）。"
        "旧数据没有该字段时为 null。",
    )


class DocumentListResponse(BaseModel):
    """某方案下的文档列表。包一层对象，与其它列表响应保持同一约定。"""

    documents: list[DocumentItem] = Field(
        default_factory=list, description="该方案下的文档（至多三份，取决于已生成到哪一步）"
    )


class DocumentSaveRequest(BaseModel):
    """用户手改正文的提交体。

    只有 `content` + 可选的 `expected_version_no`：归属由路径上的方案 id 决定、产物类型由
    路径上的 kind 决定。刻意**不接收**待确认清单——它是模型产出的元数据，不该由用户或
    客户端改写。

    `expected_version_no` 是**版本锁**（票 #81）：客户端提交时「我以为当前是第几版」
    （来自它读到的那份文档的 `version_no`）。不给时不比对（兼容旧客户端 / 内部调用）；
    给了且与库里当前版本号不一致时后端**拒绝写入**（HTTP 409 + `CONTENT_CHANGED`），
    不按「当前版本」盲改——那一版很可能已经是刚生成好的新正文，覆盖回去就是静默丢稿。
    """

    content: str = Field(description="用户编辑后的完整文档正文")
    expected_version_no: int | None = Field(
        default=None,
        description=(
            "客户端以为的当前版本序号（`documents.version_no`）。对不上时拒绝保存："
            "409 + `CONTENT_CHANGED`，界面提示「正文已被更新，请刷新后重试」。"
            "0 表示「我认为这份文档还没有任何版本」（首次手改时允许建版本 1）。"
        ),
    )


# ---------------------------------------------------------------------------
# 文档版本（document_versions）：存为新版本 / 看历史 / 恢复
# ---------------------------------------------------------------------------
# 「正文只住在版本表」之后，documents 行只是一份文档的身份 + 当前版本指针。
# 对外读文档（DocumentItem）的结构**不变**，版本是**新增**的一组读 / 写能力。


class DocumentVersionItem(BaseModel):
    """版本摘要，用于版本列表。刻意**不含正文**——列表可能很长，只给概览。"""

    version_id: int = Field(description="版本 id（供恢复引用）")
    version_no: int = Field(description="版本序号，从 1 开始、单调递增")
    source: str = Field(
        description="这一版的来源：prd / api_docs / prompts（整篇生成）、optimize / clarify（就地改写）、"
        "manual（手改）、checkpoint（用户存为新版本）、restore（恢复）、migrated（迁移回填）"
    )
    note: str = Field(default="", description="「存为新版本」时用户写的备注；其它来源为空")
    content_sha: str = Field(default="", description="内容指纹（排查用，不属于展示契约）")
    content_length: int = Field(default=0, description="正文字符数（概览用，不下发正文）")
    created_at: str = Field(description="创建时间（UTC ISO 8601）")
    is_current: bool = Field(default=False, description="是否为当前版本")
    partial: bool = Field(
        default=False,
        description=(
            "这一版是否**部分完成**（票 #85）：正文可用但不完整。版本列表据此把它与"
            "完整版本区分开，并说明它带着「已生成的部分可以先用」的性质。"
        ),
    )


class DocumentVersionDetail(BaseModel):
    """版本详情：摘要 + 正文 + 清单 + 回执。"""

    version_id: int = Field(description="版本 id")
    version_no: int = Field(description="版本序号")
    content: str = Field(default="", description="该版本文档正文（Markdown）")
    pending_items: list[dict[str, Any]] = Field(
        default_factory=list, description="该版本当时的待确认清单"
    )
    last_run_summary: dict[str, Any] | None = Field(
        default=None, description="该版本当时的用量回执；缺失为 null"
    )
    review: dict[str, Any] | None = Field(
        default=None,
        description=(
            "该版本的审核结论（一版一条）；历史版本 / 没审成时为 null（「未审核」）。"
            "形状与 `DocumentItem.review` 相同。"
        ),
    )
    review_failed_at: str | None = Field(
        default=None,
        description=(
            "该版本上一次审核试过但没跑成的时间；`None` = 没有失败记录。"
            "形状与 `DocumentItem.review_failed_at` 相同。"
        ),
    )
    source: str = Field(description="这一版的来源（见 DocumentVersionItem.source）")
    note: str = Field(default="", description="备注")
    partial: bool = Field(
        default=False,
        description=(
            "这一版是否**部分完成**（票 #85）：正文可用但不完整。"
            "与 `DocumentVersionItem.partial` 同一个后端列（`document_versions.partial`）。"
        ),
    )
    created_at: str = Field(description="创建时间（UTC ISO 8601）")
    updated_at: str = Field(description="最近更新时间（UTC ISO 8601）")
    is_current: bool = Field(default=False, description="是否为当前版本")


class DocumentVersionListResponse(BaseModel):
    """某份文档的版本列表。包一层对象，与其它列表响应保持同一约定。"""

    versions: list[DocumentVersionItem] = Field(
        default_factory=list, description="版本摘要，按版本号从新到旧"
    )


class DocumentCheckpointRequest(BaseModel):
    """把当前正文「存为一个新版本」的提交体。

    `content` 由客户端给出（取用户**眼前**这一版）——防抖窗口内用户刚敲的字可能还没落库，
    直接存库里那份会丢掉它。清单与回执则从服务端当前版本继承，不由客户端改。
    """

    content: str = Field(description="要存为新版本的完整文档正文")
    note: str = Field(default="", max_length=200, description="这一版的备注，可有可无")


class DocumentRestoreRequest(BaseModel):
    """恢复某个历史版本的提交体。"""

    version_id: int = Field(description="要恢复的历史版本 id")


class DocumentVersionDeleteResponse(BaseModel):
    """删除某个历史版本的响应。

    刻意回 `version_id`（而不仅是 `ok`）：前端可能在多处同时持有版本引用，回一个被删掉
    的 id 让调用方有机会把界面上那条记录一并收掉，不必再猜「我刚才删的是哪一版」。
    """

    ok: bool = Field(default=True, description="是否成功删除")
    version_id: int = Field(description="被删除的版本 id")
