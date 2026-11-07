"""任务「目标产物」判定（`job_service` 的拆分子模块）。

`kind` 只说「做了什么」（optimize / clarify / auto_clarify / review），产物类型要由请求体
的 `doc_kind` 决定。创建任务时写 `doc_kind` 列、收尾时决定写哪份文档、回执报哪个
技能包，用的都是同一份判断——放在单独模块里，几处不会分叉。

从 `job_service` **机械迁出**（issue #5），函数体逐字未改。
"""

from __future__ import annotations

from typing import Any

from backend.core.contracts import DOCUMENT_KINDS
from backend.core.errors import AppError, ErrorCode


def _ensure_product_kind(value: str | None, *, action: str) -> str:
    """校验「对哪一份产物做事」的取值，不合法即 400。

    为什么收成一处：`review` / `review_fix` / `clarify` 三条动作链都要在**建任务那一刻**
    拒掉缺 `doc_kind` 或取值非法的请求（猜一个默认产物会把结论、改写结果记到别人头上），
    而这句话的措辞此前在五个地方各写了一遍——改一处忘一处，用户就会看到两种说法的
    同一个错误。

    Args:
        value: 请求体里的 `doc_kind`。
        action: 这次动作的中文名（「重新审核」「按结论修复」「按澄清结果改写」），
            用来把「谁必须指定」说清楚。

    Returns:
        合法的产物种类（`prd` / `api_docs` / `prompts`）。

    Raises:
        AppError: 缺值或取值不在 `DOCUMENT_KINDS` 里（`INVALID_REQUEST`，400）。
    """
    kind = str(value or "")
    if kind in DOCUMENT_KINDS:
        return kind
    raise AppError(
        ErrorCode.INVALID_REQUEST,
        f"{action}必须指定 doc_kind（prd / api_docs / prompts），收到：{kind or '（空）'}",
    )


def _target_product_kind(kind: str, request: dict[str, Any]) -> str | None:
    """任务要**产出/改写/审核**的是哪一份产物；无法确定时返回 `None`。

    - `prd` / `api_docs` / `prompts`：整篇生成，种类本身就是产物类型。
    - `optimize` / `clarify` / `auto_clarify` / `review` / `review_fix`：对某一份**已有**
      文档的动作，产物类型来自请求体的 `doc_kind`。

    返回 `None` 表示取不到（如 `optimize` 漏传 `doc_kind`）。调用方据此**既不注入技能资源
    也不在回执里报技能包身份**——报一个跟实际注入不符的包，比不报更糟。
    """
    if kind in DOCUMENT_KINDS:
        return kind
    if kind in ("optimize", "clarify", "auto_clarify", "review", "review_fix"):
        doc_kind = request.get("doc_kind")
        return doc_kind if doc_kind in DOCUMENT_KINDS else None
    return None
