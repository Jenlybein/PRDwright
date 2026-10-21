"""方案（solutions）业务服务。

一条方案由三层组成，各自有各自的读写模式：

- **相位与草稿**（`view_state` / `chat_tab` / 粘贴的正文 …）：整份覆盖，前端是唯一写者；
- **聊天轮次**（`chat_turns`）：只追加，**服务端是唯一写者**；
- **会话 id**（`chat_sessions`）：按方案 + 线程唯一，服务端持有。

为什么不再是一份 JSON
--------------------
原先整份 `SessionDataV2` 存进一列 `data`，本层刻意不解析它——那条约定的前提是
「只有一个写者」。会话被多处访问（两台设备、切方案回切）之后，前提不成立：
整份覆盖意味着后写的把先写的全部盖掉，而聊天记录还只增不减。

拆开之后，`data` 列退役：不再写、不再读，旧值留作历史备份。

## 为什么是「有内容了才创建」

方案不是选中入口的那一刻就建，而是**首次出现非空内容**时才建（判定在前端，见
`stores/solutions.ts`）。这样用户只是点进来看一眼不会留下一堆空方案；
代价是保存分「创建 / 更新」两条路径，由前端根据是否已有 id 决定调 `create` 还是 `update`。

## 归属校验在哪一层

在 Repository 层（每个方法都强制带 `owner_id`）。
本层拿到的「查不到」既可能是方案不存在，也可能是方案不属于当前用户——**两者都按不存在处理**，
路由层统一返回 404。用 403 回应后者会泄露「这个 id 确实存在」这一信息。
"""

from __future__ import annotations

import json
from typing import Any

from backend.repositories.solution_repo import SolutionRepository

# 标题长度上限。超长标题会把侧边栏撑坏，且它只是展示用的冗余值，
# 截断没有信息损失（完整内容在草稿列里）。
_TITLE_MAX = 200
# 入口模式 / 阶段 / 线程标识的取值来自前端的联合类型，正常远小于此，
# 这里只是防止异常输入写进库里。
_MODE_MAX = 32
_STAGE_MAX = 64
_KIND_MAX = 32


def _clean(text: str, limit: int) -> str:
    """去掉首尾空白并截断到上限，避免把超长或带换行的值写进摘要列。"""
    return text.strip()[:limit]


def _state_from(payload: dict[str, Any] | None) -> dict[str, Any]:
    """把请求体里的状态对象转成仓储认识的列字典。

    `summary` 单独处理：它是一张问卷（嵌套对象 + 数组），**刻意留一列 JSON**。
    加一个问题就加一个字段，拆成列等于每次改问卷都要写迁移；
    演进快的结构放 JSON，演进慢的建列。

    其余字段是标量，原样落到各自列上。空值统一写空串 / `None`，
    不让「缺失」与「空」在库里变成两种表示法。
    """
    state = payload or {}
    summary = state.get("summary")
    return {
        "view_state": _clean(str(state.get("view_state") or ""), _STAGE_MAX),
        "entry_mode": _clean(str(state.get("entry_mode") or ""), _MODE_MAX),
        "chat_tab": _clean(str(state.get("chat_tab") or ""), _STAGE_MAX),
        "summary_json": json.dumps(summary, ensure_ascii=False)
        if isinstance(summary, dict) and summary
        else "",
        "quick_product_name": _clean(str(state.get("quick_product_name") or ""), _TITLE_MAX),
        "quick_requirements": str(state.get("quick_requirements") or ""),
        "draft_prd_text": str(state.get("draft_prd_text") or ""),
        "paste_prd_text": str(state.get("paste_prd_text") or ""),
        "paste_api_docs_text": str(state.get("paste_api_docs_text") or ""),
        "active_job_id": state.get("active_job_id")
        if isinstance(state.get("active_job_id"), int)
        else None,
        "active_job_kind": _clean(str(state.get("active_job_kind") or ""), _KIND_MAX),
        "active_job_doc_kind": _clean(str(state.get("active_job_doc_kind") or ""), _KIND_MAX),
    }


def create_solution(
    owner_id: int,
    title: str,
    mode: str,
    stage: str,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """新建方案，返回其摘要（不含状态列）。"""
    solution_id = SolutionRepository.create(
        owner_id=owner_id,
        title=_clean(title, _TITLE_MAX),
        mode=_clean(mode, _MODE_MAX),
        stage=_clean(stage, _STAGE_MAX),
        state=_state_from(state),
    )
    created = SolutionRepository.get(owner_id=owner_id, solution_id=solution_id)
    if created is None:  # 刚插入就读不到，属于异常，不该静默返回半个对象
        raise RuntimeError("方案创建失败，请稍后重试")
    return _to_summary(created)


def update_solution(
    owner_id: int,
    solution_id: int,
    title: str,
    mode: str,
    stage: str,
    state: dict[str, Any] | None,
    content_changed: bool,
) -> dict[str, Any] | None:
    """整份覆盖某方案的状态列；不存在或不属于当前用户时返回 None（由路由层翻成 404）。

    `content_changed` 由前端给出，本层只转手：**只有前端知道这次改动到底动了什么**——
    它的 `contentTick` 由「需求摘要 / 草稿 / 对话」的变更驱动，而「翻了一页」
    （`viewState`）不计入。让后端去猜只能靠 diff，那会把前端契约焊死在后端。
    """
    ok = SolutionRepository.update(
        owner_id=owner_id,
        solution_id=solution_id,
        title=_clean(title, _TITLE_MAX),
        mode=_clean(mode, _MODE_MAX),
        stage=_clean(stage, _STAGE_MAX),
        state=_state_from(state),
        content_changed=content_changed,
    )
    if not ok:
        return None
    return get_solution(owner_id, solution_id)


def get_solution(owner_id: int, solution_id: int) -> dict[str, Any] | None:
    """读取方案详情（含状态列）；不存在或不属于当前用户时返回 None。"""
    row = SolutionRepository.get(owner_id=owner_id, solution_id=solution_id)
    return dict(row) if row else None


def list_solutions(owner_id: int) -> list[dict[str, Any]]:
    """列出当前用户的方案摘要，最近更新在前。

    走 Repository 的摘要查询，**不读状态列与草稿列**。
    """
    return [_to_summary(row) for row in SolutionRepository.list_by_owner(owner_id)]


def delete_solution(owner_id: int, solution_id: int) -> bool:
    """删除方案；不存在或不属于当前用户时返回 False。

    聊天轮次与会话 id 由库里的 `ON DELETE CASCADE` 一起带走。
    """
    return SolutionRepository.delete(owner_id=owner_id, solution_id=solution_id)


def solution_exists(owner_id: int, solution_id: int) -> bool:
    """方案是否存在且属于当前用户。

    给文档接口用：它要在返回空列表之前区分「这份方案不属于你」（→404）与
    「它确实还没有文档」（→200 空列表）。路由层不直接碰仓储，所以在这里转一手。
    """
    return SolutionRepository.exists(owner_id=owner_id, solution_id=solution_id)


def state_of(row: dict[str, Any]) -> dict[str, Any]:
    """把库行的状态列还原成前端认识的形状。

    `summary_json` 反序列化失败时退化为 `None` 而不是抛错：一份坏掉的问卷不该让
    整份方案打不开——其他状态（草稿、草稿、进度）还是好的。

    空串一律还原成 `None` / `""` 的语义由调用方决定，这里只保证**形状稳定**。
    """
    raw_summary = row.get("summary_json") or ""
    summary: dict[str, Any] | None = None
    if isinstance(raw_summary, str) and raw_summary:
        try:
            parsed = json.loads(raw_summary)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            summary = parsed
    return {
        "view_state": row.get("view_state") or "",
        "entry_mode": row.get("entry_mode") or "",
        "chat_tab": row.get("chat_tab") or "",
        "summary": summary,
        "quick_product_name": row.get("quick_product_name") or "",
        "quick_requirements": row.get("quick_requirements") or "",
        "draft_prd_text": row.get("draft_prd_text") or "",
        "paste_prd_text": row.get("paste_prd_text") or "",
        "paste_api_docs_text": row.get("paste_api_docs_text") or "",
        "active_job_id": row.get("active_job_id"),
        "active_job_kind": row.get("active_job_kind") or "",
        "active_job_doc_kind": row.get("active_job_doc_kind") or "",
    }


def _to_summary(row: dict[str, Any]) -> dict[str, Any]:
    """把库行裁剪成列表 / 详情共用的摘要字段。

    只挑需要的列而不是 `dict(row)` 再删 key：将来表里加列（哪怕是比较大的字段）时，
    列表接口不会被无声地带上新字段。
    """
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "mode": row["mode"],
        "stage": row["stage"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        # 两根时间轴都要给出去：界面显示的是 `content_updated_at`（作品何时真的变了），
        # 而 `updated_at`（记录何时被写过）留给诊断，别让前端为了拿它再打一次接口。
        "content_updated_at": row["content_updated_at"],
    }
