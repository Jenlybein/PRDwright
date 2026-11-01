"""任务「目标产物」判定（`job_service` 的拆分子模块）。

`kind` 只说「做了什么」（optimize / clarify / auto_clarify），产物类型要由请求体
的 `doc_kind` 决定。创建任务时写 `doc_kind` 列、收尾时决定写哪份文档、回执报哪个
技能包，用的都是同一份判断——放在单独模块里，几处不会分叉。

从 `job_service` **机械迁出**（issue #5），函数体逐字未改。
"""

from __future__ import annotations

from typing import Any

from backend.core.contracts import DOCUMENT_KINDS


def _target_product_kind(kind: str, request: dict[str, Any]) -> str | None:
    """任务要**产出/改写**的是哪一份产物；无法确定时返回 `None`。

    - `prd` / `api_docs` / `prompts`：整篇生成，种类本身就是产物类型。
    - `optimize` / `clarify` / `auto_clarify`：对某一份**已有**文档的动作，产物类型来自请求体的 `doc_kind`。

    返回 `None` 表示取不到（如 `optimize` 漏传 `doc_kind`）。调用方据此**既不注入技能资源
    也不在回执里报技能包身份**——报一个跟实际注入不符的包，比不报更糟。
    """
    if kind in DOCUMENT_KINDS:
        return kind
    if kind in ("optimize", "clarify", "auto_clarify"):
        doc_kind = request.get("doc_kind")
        return doc_kind if doc_kind in DOCUMENT_KINDS else None
    return None
