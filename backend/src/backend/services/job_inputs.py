"""文档类任务的输入解析：**一律由服务端按方案从库里取**（spec #51 / 票 #53 / #54）。

为什么归服务端
--------------
上游正文从前由前端随请求体传（生成接口文档传 PRD、生成提示词传 PRD + 接口文档、
生成 PRD 传摘要与澄清历史）。后端同时维护着同一份「当前版本」，两份真相迟早不一致：
上游刚改过，生成却可能用的还是旧稿，而且没有任何一处能发现这件事（见 ADR 0002 第 3 条）。

解析时机
--------
**建任务那一刻**（`POST /api/jobs` 里、造单之前），结果随任务单定格、派发时合进运行时
手里的那份请求参数。落库的 `request_params`（票 #34 的不可变投影）一个字都不动：
它记录的是「调用方当时发了什么」，这里回答的是「这一刻库里是什么」——两者不同是设计
如此，不是没同步。

为什么要建任务那一刻而不是开流那一刻（#54 的取舍）
--------------------------------------------------
「缺方案 / 缺材料」必须是**明确的 400**，而不是「任务建起来了、随后在流里失败」：后者
会在用户的任务列表里留下一条失败记录，还白占一次额度。因此解析提前到请求线程里做，
出错就是一次货真价实的 HTTP 400（不空跑一次生成）。

取哪些（不存在兜底）
--------------------
- 接口文档 → PRD 当前版本全文，其次方案上保存的录入文本；
- 提示词 → PRD 与接口文档当前版本全文（接口文档可选），其次录入文本；
- PRD → 方案上的结构化需求摘要、快捷流程的自由文本需求、需求澄清线程（`requirement`）；
- 按澄清结果改写 → 该文档那条澄清线程（`prd-clarify` / `api-docs-clarify` /
  `prompts-clarify`）；
- 导入入口（我有 PRD / 我有 PRD + 接口文档）→ 方案上保存的录入文本列；
- 重新审核 → 被审的那一份当前正文 + 与生成时同一份上游材料（只审不改，见 ADR 0002）；
- 按结论修复 → 被修的那一份当前正文 + **当前版本那条结论** + 上游材料全文
  （与重新审核同源，但上游不做骨架化：修复要照着原文改，见 ADR 0002）。

**取不到就 400**：请求体里那一份不再参与（#53 的过渡兜底在本票删除），否则双真源
只是换了个地方复活。唯一的例外是「重新审核的**上游**」：它只是判定时的对照材料，
缺了还能按「仅依据正文与写作规范核对」降级（见 `_review_inputs`）——而「旧方案也能补审」
是明确的用户故事，不该被一条与生成共用的硬校验挡在门外。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from backend.core.engine import session_scope
from backend.core.errors import AppError, ErrorCode
from backend.repositories.chat_turn_repo import ChatTurnRepository, to_history_item
from backend.repositories.solution_repo import SolutionRepository
from backend.services import document_store

logger = logging.getLogger(__name__)

# 需要方案、且输入由服务端解析的任务种类。`chat` 不在此列：它的上下文是对话线程本身，
# 由 `job_service_impl._chat_events` 从 `chat_turns` 拼。
DOCUMENT_KINDS: tuple[str, ...] = (
    "prd",
    "api_docs",
    "prompts",
    "optimize",
    "clarify",
    "auto_clarify",
    "review",
    "review_fix",
)

# 任务种类 → 给人看的名字（缺材料时的提示要能直接读懂缺的是哪一步）。
_KIND_LABELS: dict[str, str] = {
    "prd": "PRD 生成",
    "api_docs": "接口文档生成",
    "prompts": "提示词生成",
    "optimize": "文档优化",
    "clarify": "按澄清结果改写",
    "auto_clarify": "AI 自行澄清",
    "review": "重新审核",
    "review_fix": "按结论修复",
}

# 文档类型 → 它自己的那条澄清线程（四条线程之一，见 `core/chat_kinds.py`）。
_CLARIFY_STAGES: dict[str, str] = {
    "prd": "prd-clarify",
    "api_docs": "api-docs-clarify",
    "prompts": "prompts-clarify",
}

# 产物类型 → 给人看的名字（「这一份还没有正文」这类提示要能直接读懂说的是谁）。
_DOC_KIND_LABELS: dict[str, str] = {
    "prd": "PRD",
    "api_docs": "接口文档",
    "prompts": "提示词套件",
}

# 需求澄清线程的标识（PRD 的上游之一）。
_REQUIREMENT_STAGE = "requirement"

# 缺材料时的下一步：生成上游，或（导入入口）把材料存到方案上。
# 「请先保存材料」是验收标准逐字要求出现在提示里的那一句，别改写。
_SAVE_MATERIAL_HINT = "请先生成 PRD；若走导入入口，请先保存材料。"


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
    kind: str,
    owner_id: int,
    solution_id: int | None,
    doc_kind: str | None = None,
) -> dict[str, Any]:
    """按任务种类解析输入，返回**要合进运行时请求参数**的那几格；缺材料即 400。

    只认文档类任务：`chat` 返回空（它的上下文由对话服务从轮次表拼）。`optimize` /
    `auto_clarify` 的输入（要改的那份正文、指令、清单）仍由调用方给——它们描述的是
    「这一次要做什么」，不是「上游是谁」。`clarify` 的对话记录、`review` 的被审正文与
    `review_fix` 的被修正文（外加那条结论）同属上游，一律在这里取。

    Args:
        kind: 任务种类。
        owner_id: 当前用户；读取一律带它，越权与不存在等价。
        solution_id: 方案 id；文档类任务必填，缺了就是 400。
        doc_kind: 目标产物（`clarify` 用它定位澄清线程；`review` / `review_fix` 用它决定
            审 / 修哪一份，`review_fix` 还用它决定注入哪一份上游全文）。

    Raises:
        AppError: 缺方案（`INVALID_REQUEST`）、方案不可见（`SOLUTION_NOT_FOUND`）、
            或服务端取不到必需材料（`INVALID_REQUEST`，文案说明缺什么）。
    """
    if kind not in DOCUMENT_KINDS:
        return {}
    if solution_id is None:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"{_KIND_LABELS.get(kind, kind)}必须关联一份方案（solution_id）",
        )

    if kind == "prd":
        return _prd_inputs(owner_id, solution_id)
    if kind == "api_docs":
        return {"prd_content": _upstream_prd(owner_id, solution_id, "接口文档生成")}
    if kind == "prompts":
        return _prompts_inputs(owner_id, solution_id)
    if kind == "clarify":
        return {"history": _clarify_history(owner_id, solution_id, doc_kind)}
    if kind == "review":
        return _review_inputs(owner_id, solution_id, doc_kind)
    if kind == "review_fix":
        return _fix_inputs(owner_id, solution_id, doc_kind)
    return {}


def prd_requirement_text(resolved: dict[str, Any]) -> str:
    """从**已解析**的输入里取用于计算输入指纹的需求文本。

    与真正喂给模型的那份同源：指纹若仍按请求体算，就会出现「内容取自库里、指纹取自
    前端副本」这种自相矛盾的记录——上游改过一版，指纹却还是旧的。取法沿用既有口径：
    自由文本优先，否则用结构化摘要的 JSON。
    """
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


def _picked_prd(contents: dict[str, str], solution: _SolutionInputs) -> str:
    """上游 PRD 的取法（**不校验**）：库内当前版本优先，其次方案上保存的录入文本。

    单独一格是给「重新审核」用的：那里上游只是对照材料，缺了按降级处理而不是 400。
    """
    return _first_non_empty(contents.get("prd", ""), solution.pasted_prd)


def _picked_api_docs(contents: dict[str, str], solution: _SolutionInputs) -> str:
    """上游接口文档的取法（可选材料）：库内当前版本优先，其次方案上的录入文本。"""
    return _first_non_empty(contents.get("api_docs", ""), solution.pasted_api_docs)


def _upstream_prd(owner_id: int, solution_id: int, label: str) -> str:
    """接口文档 / 提示词的上游 PRD：库内当前版本 → 方案上的录入文本 → 400。"""
    contents = _document_contents(owner_id, solution_id)
    solution = _require_solution(owner_id, solution_id)
    prd = _picked_prd(contents, solution)
    if not prd:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"{label}缺少上游 PRD：库里没有 PRD 当前版本，方案上也没有保存的 PRD 材料。"
            f"{_SAVE_MATERIAL_HINT}",
        )
    return prd


def _prompts_inputs(owner_id: int, solution_id: int) -> dict[str, Any]:
    """提示词的上游：PRD 与接口文档两份，各自独立解析。

    接口文档是**可选**材料（「我有 PRD」入口就只有 PRD），缺了按「只有 PRD」处理；
    PRD 缺了则没有可写的提示词，直接 400。
    """
    contents = _document_contents(owner_id, solution_id)
    solution = _require_solution(owner_id, solution_id)
    prd = _picked_prd(contents, solution)
    if not prd:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "提示词生成缺少上游 PRD：库里没有 PRD 当前版本，方案上也没有保存的 PRD 材料。"
            f"{_SAVE_MATERIAL_HINT}",
        )
    return {
        "prd_content": prd,
        # 可选材料：取不到就是空串，生成按「只有 PRD」处理（与从前口径一致）
        "api_docs_content": _picked_api_docs(contents, solution),
    }


def _review_inputs(
    owner_id: int, solution_id: int, doc_kind: str | None
) -> dict[str, Any]:
    """重新审核的输入：**被审的那一份当前正文** + 它比对用的上游材料。

    `document` 一格是被审的正文；它与「上游材料」不同——不参与生成本身，只作为审核对象。
    **取不到就直接 400**：没有正文就没有「被审的那一版」，结论也无处可写
    （`document_store.commit_review` 写的是当前版本上的那一列）。这里刻意**不**回退到
    方案上的录入文本：那份材料还没有变成任何一版正文，审它等于给一条不存在的正文发结论。

    上游材料按三份产物各自的口径取（C-R3：只来自服务端）：

    - PRD → 结构化摘要 / 自由文本需求 + 需求澄清历史（按章重审要拿它核对「有没有编造」）；
    - 接口文档 → PRD 当前版本全文（其次方案上的录入文本）；
    - 提示词 → PRD 与接口文档当前版本全文（其次方案上的录入文本）。

    与生成的区别只有一点：**上游缺失不 400**。重审的产出是「这一版被审成什么样」，
    上游只是对照材料；缺了它会按「仅依据正文与写作规范核对」降级（提示词里写明），
    而「历史方案也能补审」是明确的用户故事，不该被生成侧那条硬校验挡在门外。
    """
    kind = str(doc_kind or "")
    if kind not in ("prd", "api_docs", "prompts"):
        # 措辞与 `clarify` 的同名校验一致：不猜默认产物，猜错等于把结论记到别人头上
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "重新审核必须指定 doc_kind（prd / api_docs / prompts）",
        )

    contents = _document_contents(owner_id, solution_id)
    solution = _require_solution(owner_id, solution_id)
    document = _first_non_empty(contents.get(kind, ""))
    if not document:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"{_DOC_KIND_LABELS.get(kind, kind)}还没有正文，无法重新审核。"
            "请先生成这一份文档，再来审阅页重审。",
        )

    inputs: dict[str, Any] = {"document": document}
    if kind == "prd":
        # 需求输入整条链路都可能为空（导入的历史方案）：给了就用，没给就留空降级
        history = _thread_history(owner_id, solution_id, _REQUIREMENT_STAGE)
        inputs.update(
            {
                "summary": solution.summary,
                "requirements": str(solution.quick_requirements or ""),
                "product_name": solution.quick_product_name,
                "history": history or None,
            }
        )
    else:
        inputs["prd_content"] = _picked_prd(contents, solution)
        if kind == "prompts":
            inputs["api_docs_content"] = _picked_api_docs(contents, solution)
    return inputs


def _fix_inputs(
    owner_id: int, solution_id: int, doc_kind: str | None
) -> dict[str, Any]:
    """**按审核结论修复**的输入：被修的那一版正文 + 那条结论 + 上游材料全文。

    与 `_review_inputs` 同构（同一份「当前版本」+ 同一份上游），只多一格**结论**，
    却因此多出两条硬校验——修复不是「再写一遍」，它只对一件事开放：

    - **这一份必须已经有正文**：没有正文就没有「被审的那一版」，也就不存在修复对象；
    - **当前版本的结论必须是「有问题」**：修复入口在界面上就只对「有问题」开放
      （ARCHITECTURE §3.8）。这里同样把关，是因为**界面的可用条件不是权限**——
      一次迟到的、或绕过界面发来的请求，否则会把一份审核通过的正文悄悄换掉。

    上游一律**全文**（不是审核侧那份骨架）：修复要照着上游改，骨架会丢掉散文里的事实。
    上游缺失不硬拦（PRD 尤其可能没有需求输入），由提示词里那句「未提供」显式降级；
    但接口文档 / 提示词缺 PRD 时仍然 400——没有 PRD 就没有事实来源，重写等于编造。
    """
    kind = str(doc_kind or "")
    if kind not in ("prd", "api_docs", "prompts"):
        # 措辞与 `clarify` / `review` 的同名校验一致：不猜默认产物
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "按结论修复必须指定 doc_kind（prd / api_docs / prompts）",
        )

    rows = document_store.list_documents(owner_id, solution_id)
    contents = {row["kind"]: str(row.get("content") or "") for row in rows}
    reviews = {row["kind"]: row.get("review") for row in rows}
    solution = _require_solution(owner_id, solution_id)
    label = _DOC_KIND_LABELS.get(kind, kind)

    document = _first_non_empty(contents.get(kind, ""))
    if not document:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"{label}还没有正文，无法按结论修复。请先生成这一份文档，再来审阅页修复。",
        )
    conclusion = reviews.get(kind)
    verdict = ""
    if isinstance(conclusion, dict):
        verdict = str(conclusion.get("verdict") or "").strip().lower()
    if verdict != "revise":
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"{label}当前版本的审核结论不是「有问题」，无法按结论修复。"
            "请先重新审核，确认确实有问题后再修复。",
        )

    inputs: dict[str, Any] = {"document": document, "review_conclusion": conclusion}
    if kind == "prd":
        # PRD 的上下文窗口：结构化摘要 / 自由文本需求 + 两条澄清线程 + 当前正文 + 结论。
        # 需求输入整条链路都可能为空（导入的历史方案），给了就用、没给由提示词显式降级。
        inputs.update(
            {
                "summary": solution.summary,
                "requirements": str(solution.quick_requirements or ""),
                "product_name": solution.quick_product_name,
                "history": _thread_history(owner_id, solution_id, _REQUIREMENT_STAGE) or None,
                "clarify_history": _thread_history(owner_id, solution_id, "prd-clarify") or None,
            }
        )
    else:
        prd = _picked_prd(contents, solution)
        if not prd:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                f"{label}修复缺少上游 PRD：库里没有 PRD 当前版本，方案上也没有保存的 PRD 材料。"
                f"{_SAVE_MATERIAL_HINT}",
            )
        inputs["prd_content"] = prd
        if kind == "prompts":
            inputs["api_docs_content"] = _picked_api_docs(contents, solution)
    return inputs


def _prd_inputs(owner_id: int, solution_id: int) -> dict[str, Any]:
    """PRD 的上游：结构化摘要 / 自由文本需求 + 需求澄清历史。"""
    solution = _require_solution(owner_id, solution_id)
    requirements = str(solution.quick_requirements or "")
    if not solution.summary and not requirements.strip():
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "PRD 生成缺少需求输入：方案上既没有结构化摘要，也没有自由文本需求。"
            "请先填写需求后再生成。",
        )
    history = _thread_history(owner_id, solution_id, _REQUIREMENT_STAGE)
    return {
        "summary": solution.summary,
        "requirements": requirements,
        "product_name": solution.quick_product_name,
        # 结构化摘要里也可能带产品名：摘要是权威，这里只把方案上的那一列递过去
        "history": history or None,
    }


def _clarify_history(owner_id: int, solution_id: int, doc_kind: str | None) -> list[dict[str, Any]]:
    """按澄清结果改写的上游：该文档那条澄清线程的全部轮次。

    从前这条对话由前端随请求体回传。问题与上游正文同构：前端只是服务端轮次的镜像，
    镜像落后一版（刷新没拉全 / 另一台设备聊过）时，模型就会对着旧结论改写。
    这里按 `doc_kind` 定位线程，投影成 `{role, content}`——与前端当初回传的形状一致。
    """
    stage = _CLARIFY_STAGES.get(str(doc_kind or ""))
    if stage is None:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "按澄清结果改写必须指定 doc_kind（prd / api_docs / prompts）",
        )
    turns = _thread_history(owner_id, solution_id, stage)
    history = [{"role": item["role"], "content": item["content"]} for item in turns]
    if not any(str(item["content"]).strip() for item in history):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"按澄清结果改写需要这条文档的澄清对话，但它还没有任何记录（线程 {stage}）。"
            "请先在对话页把待确认问题聊出结论。",
        )
    return history


# ---------------------------------------------------------------------------
# 读取（每次都是短事务；本模块不持有事务跨 await）
# ---------------------------------------------------------------------------


def _require_solution(owner_id: int, solution_id: int) -> _SolutionInputs:
    """读方案行上与生成上游有关的几列；方案不可见时按 404 报「方案不存在」。

    与登记那一步的判据逐字一致（`id + owner`）：不在这里区分「不存在」与「不是你的」。
    """
    with session_scope() as session:
        row = SolutionRepository(session).get(owner_id, solution_id)
        if row is None:
            raise AppError(ErrorCode.SOLUTION_NOT_FOUND, "方案不存在")
        return _SolutionInputs(
            summary=_parse_summary(row.summary_json),
            quick_requirements=str(row.quick_requirements or ""),
            quick_product_name=str(row.quick_product_name or ""),
            pasted_prd=_first_non_empty(row.paste_prd_text or "", row.draft_prd_text or ""),
            pasted_api_docs=str(row.paste_api_docs_text or ""),
        )


def _thread_history(owner_id: int, solution_id: int, stage: str) -> list[dict[str, Any]]:
    """读某条澄清线程的全部轮次，转成提示词认识的历史项。

    整条线程都算上游：开场白（`system`，含问卷答案 / 文档与待确认清单）、用户补充、
    AI 追问与作答（`tool`）都是「这件事是怎么定的」的一部分，只取用户那几条会丢掉结论。
    """
    with session_scope() as session:
        turns = ChatTurnRepository(session).list_thread(owner_id, solution_id, stage)
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


__all__ = [
    "DOCUMENT_KINDS",
    "prd_requirement_text",
    "resolve_runtime_inputs",
]
