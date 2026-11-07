"""生成任务的上游输入：一律由服务端按任务种类从库里取（spec #51 / 票 #53）。

为什么归服务端
--------------
上游正文从前由前端随请求体传（生成接口文档传 PRD、生成提示词传 PRD + 接口文档、
生成 PRD 传摘要与澄清历史）。后端同时维护着同一份「当前版本」，两份真相迟早不一致：
上游刚改过，生成却可能用的还是旧稿，而且没有任何一处能发现这件事（见 ADR 0002 第 3 条）。

解析时机
--------
**建事件流那一刻**（`run_job` 开流之前）把结果合进运行时手里的那份请求参数。
落库的 `request_params`（票 #34 的不可变投影）一个字都不动：它记录的是「调用方当时发了
什么」，这里回答的是「这一刻库里是什么」——两者不同是设计如此，不是没同步。

取哪些
------
- 接口文档 → PRD 当前版本全文；
- 提示词 → PRD 与接口文档当前版本全文；
- PRD → 方案上的结构化需求摘要、快捷流程的自由文本需求、需求澄清线程（`requirement`）；
- 导入入口（我有 PRD / 我有 PRD + 接口文档）→ 方案上保存的录入文本列（库内产物为空时）；
- 重新审核 → 被审的那一份当前正文 + 与生成时同一份上游材料（只审不改，见 ADR 0002）。

**取不到时沿用请求体里的值**：本票只做「扩」，缺材料的 400 归下一票 #54。这条兜底
刻意只写在本模块一处——#54 要删的就是它。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from backend.core.engine import session_scope
from backend.repositories.chat_turn_repo import ChatTurnRepository, to_history_item
from backend.repositories.solution_repo import SolutionRepository
from backend.services import document_store

logger = logging.getLogger(__name__)

# 需求澄清线程的标识（四条线程之一，见 `core/chat_kinds.py`）。
_REQUIREMENT_STAGE = "requirement"


@dataclass(frozen=True, slots=True)
class _SolutionInputs:
    """方案行上属于「生成上游」的那几列。

    `summary` 是解析后的结构化摘要（`solutions.summary_json` 解不开时为 `None`）；
    `pasted_prd` 是导入入口存下来的 PRD 录入文本，按 `paste_prd_text` →
    `draft_prd_text` 的顺序取第一个非空——同一份方案只会走过一个导入入口，
    正常情况下只有一列有值，顺序只是为了「两列都有时答案也确定」。
    """

    summary: dict[str, Any] | None
    quick_requirements: str
    quick_product_name: str
    pasted_prd: str
    pasted_api_docs: str


def resolve_runtime_inputs(
    kind: str, owner_id: int, solution_id: int | None, request: dict[str, Any]
) -> dict[str, Any]:
    """按任务种类解析上游输入，返回**要合进运行时请求参数**的那几格。

    只认三种整篇生成 + 重新审核：`optimize` / `clarify` / `auto_clarify` 的输入
    （要改的那份正文、指令、清单）仍由调用方给——它们描述的是「这一次要做什么」，
    不是「上游是谁」。
    """
    if solution_id is None:
        # 没有方案就没有服务端上游可查（如「无方案」逃生口）：请求体原样用，
        # 与引入本模块之前的行为一致。
        return {}
    if kind == "prd":
        return _prd_inputs(owner_id, solution_id, request)
    if kind == "api_docs":
        return {"prd_content": _api_docs_prd(owner_id, solution_id, request)}
    if kind == "prompts":
        return _prompts_inputs(owner_id, solution_id, request)
    if kind == "review":
        return _review_inputs(owner_id, solution_id, request)
    return {}


def prd_requirement_text(
    owner_id: int, solution_id: int | None, request: dict[str, Any]
) -> str:
    """取用于计算输入指纹的需求文本——**与真正喂给模型的那份同源**。

    指纹若仍按请求体算，就会出现「内容取自库里、指纹取自前端副本」这种自相矛盾的
    记录：上游改过一版，指纹却还是旧的。取法沿用既有口径：自由文本优先，
    否则用结构化摘要的 JSON。
    """
    resolved = (
        request
        if solution_id is None
        else _prd_requirement_inputs(owner_id, solution_id, request)
    )
    requirements = str(resolved.get("requirements") or "")
    if requirements.strip():
        return requirements
    summary = resolved.get("summary")
    if isinstance(summary, dict) and summary:
        return json.dumps(summary, ensure_ascii=False)
    return ""


# ---------------------------------------------------------------------------
# 各产物种类
# ---------------------------------------------------------------------------


def _api_docs_prd(owner_id: int, solution_id: int, request: dict[str, Any]) -> str:
    """接口文档的上游 PRD：库内当前版本 → 方案上的录入文本 → 请求体（过渡兜底）。"""
    contents = _document_contents(owner_id, solution_id)
    solution = _solution_inputs(owner_id, solution_id)
    return _first_non_empty(
        contents.get("prd", ""),
        solution.pasted_prd if solution is not None else "",
        str(request.get("prd_content") or ""),
    )


def _prompts_inputs(
    owner_id: int, solution_id: int, request: dict[str, Any]
) -> dict[str, Any]:
    """提示词的上游：PRD 与接口文档两份当前版本，各自独立解析。

    导入入口（我有 PRD / 我有 PRD + 接口文档）没有库内产物，材料就在方案行的录入文本里；
    两份各自回退，避免「PRD 取到了、接口文档却因为没生成过而留空」。
    """
    contents = _document_contents(owner_id, solution_id)
    solution = _solution_inputs(owner_id, solution_id)
    pasted_prd = solution.pasted_prd if solution is not None else ""
    pasted_api_docs = solution.pasted_api_docs if solution is not None else ""
    return {
        "prd_content": _first_non_empty(
            contents.get("prd", ""), pasted_prd, str(request.get("prd_content") or "")
        ),
        "api_docs_content": _first_non_empty(
            contents.get("api_docs", ""),
            pasted_api_docs,
            str(request.get("api_docs_content") or ""),
        ),
    }


def _review_inputs(
    owner_id: int, solution_id: int, request: dict[str, Any]
) -> dict[str, Any]:
    """重新审核的输入：**被审的那一份当前正文** + 它比对用的上游材料。

    三份产物的上游材料各按生成时的同一口径取（C-R3：输入只来自服务端）：

    - PRD → 结构化摘要 / 自由文本需求 + 需求澄清历史（按章重审要拿它核对「有没有编造」）；
    - 接口文档 → PRD 当前版本全文；
    - 提示词 → PRD 与接口文档当前版本全文。

    `document` 一格是**被审的正文**：与上游材料不同，它不参与生成，只作为审核对象。
    取不到（库里还没有这一份）时留空串，由审核链路给出明确的失败原因——
    绝不回退到请求体：重审的请求体本来就不携带正文。
    """
    doc_kind = str(request.get("doc_kind") or "")
    inputs: dict[str, Any] = {
        "document": _document_contents(owner_id, solution_id).get(doc_kind, "")
    }
    if doc_kind == "prd":
        inputs.update(_prd_inputs(owner_id, solution_id, request))
    elif doc_kind == "api_docs":
        inputs["prd_content"] = _api_docs_prd(owner_id, solution_id, request)
    elif doc_kind == "prompts":
        inputs.update(_prompts_inputs(owner_id, solution_id, request))
    return inputs


def _prd_inputs(
    owner_id: int, solution_id: int, request: dict[str, Any]
) -> dict[str, Any]:
    """PRD 的上游：结构化摘要 / 自由文本需求 + 需求澄清历史。"""
    inputs = dict(_prd_requirement_inputs(owner_id, solution_id, request))
    history = _requirement_history(owner_id, solution_id)
    if not history:
        # 方案上没有澄清轮次（快捷流程本来就不问问题；旧快照里可能还留着当时的结论）
        history = _request_history(request)
    inputs["history"] = history or None
    return inputs


def _prd_requirement_inputs(
    owner_id: int, solution_id: int, request: dict[str, Any]
) -> dict[str, Any]:
    """PRD 的需求输入三格（摘要 / 自由文本 / 产品名），不读澄清历史。

    单独一格的用处是输入指纹：它只需要需求文本，没必要为它多查一次轮次表。
    """
    solution = _solution_inputs(owner_id, solution_id)
    summary = solution.summary if solution is not None else None
    requirements = solution.quick_requirements if solution is not None else ""
    if not summary and not str(requirements).strip():
        # 服务端还没有需求输入：退回请求体（#54 会把这条兜底换成明确 400）
        raw_summary = request.get("summary")
        summary = raw_summary if isinstance(raw_summary, dict) and raw_summary else None
        requirements = str(request.get("requirements") or "")
    product_name = (solution.quick_product_name if solution is not None else "") or str(
        request.get("product_name") or ""
    )
    return {
        "summary": summary,
        "requirements": str(requirements or ""),
        "product_name": product_name,
    }


# ---------------------------------------------------------------------------
# 读取（每次都是短事务；本模块不持有事务跨 await）
# ---------------------------------------------------------------------------


def _solution_inputs(owner_id: int, solution_id: int) -> _SolutionInputs | None:
    """读方案行上与生成上游有关的几列；方案不可见时返回 None。"""
    with session_scope() as session:
        row = SolutionRepository(session).get(owner_id, solution_id)
        if row is None:
            return None
        pasted_prd = _first_non_empty(row.paste_prd_text or "", row.draft_prd_text or "")
        return _SolutionInputs(
            summary=_parse_summary(row.summary_json),
            quick_requirements=str(row.quick_requirements or ""),
            quick_product_name=str(row.quick_product_name or ""),
            pasted_prd=pasted_prd,
            pasted_api_docs=str(row.paste_api_docs_text or ""),
        )


def _requirement_history(owner_id: int, solution_id: int) -> list[dict[str, Any]]:
    """读需求澄清线程的全部轮次，转成提示词认识的历史项。

    整条线程都算上游：开场白（`system`，含问卷答案）、用户补充、AI 追问与作答
    （`tool`）都是「这次需求是怎么定的」的一部分，只取用户那几条会丢掉结论。
    """
    with session_scope() as session:
        turns = ChatTurnRepository(session).list_thread(
            owner_id, solution_id, _REQUIREMENT_STAGE
        )
        return [to_history_item(turn) for turn in turns]


def _document_contents(owner_id: int, solution_id: int) -> dict[str, str]:
    """读方案下三份产物的当前正文（按 kind 索引；没有的产物缺席）。"""
    return {
        row["kind"]: str(row.get("content") or "")
        for row in document_store.list_documents(owner_id, solution_id)
    }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _parse_summary(raw: Any) -> dict[str, Any] | None:
    """解析 `solutions.summary_json`；空值 / 坏 JSON / 非对象一律当「没有摘要」。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning(
            "方案的结构化摘要不是合法 JSON，本次按「没有摘要」处理",
            extra={"event": "job_inputs.bad_summary"},
        )
        return None
    return parsed if isinstance(parsed, dict) and parsed else None


def _first_non_empty(*candidates: str) -> str:
    """取第一个非空白候选；全空时返回空串（调用方据此走下一层回退）。"""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""


def _request_history(request: dict[str, Any]) -> list[dict[str, Any]]:
    """把请求体里的历史摊成普通 list（缺失 / 形状不对时为空）。"""
    raw = request.get("history")
    if isinstance(raw, (list, tuple)):
        return [dict(item) for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        return [dict(raw)]
    return []


__all__ = ["prd_requirement_text", "resolve_runtime_inputs"]
