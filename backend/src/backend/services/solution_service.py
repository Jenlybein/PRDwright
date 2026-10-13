"""方案（solutions）业务服务。

一条方案就是一份完整会话的**全量快照**（前端 `SessionDataV2` 的 JSON）。这里没有
「把方案拆成消息 / 文档分别存取」的逻辑——那需要与全量状态做双向同步，而 V2 前端
的心智模型本来就是「整份 state 一次性恢复」，拆表只会凭空多出一层同步负担。

## 为什么是「有内容了才创建」

方案不是选中入口的那一刻就建，而是**首次出现非空内容**时才建（判定在前端，见
`stores/solutions.ts`）。这样用户只是点进来看一眼不会留下一堆空方案；
代价是保存分「创建 / 更新」两条路径，由前端根据是否已有 id 决定调 `create` 还是 `update`。

## 归属校验在哪一层

在 Repository 层（`SolutionRepository` 的每个方法都强制带 `owner_id`）。
本层拿到的「查不到」既可能是方案不存在，也可能是方案不属于当前用户——**两者都按不存在处理**，
路由层统一返回 404。用 403 回应后者会泄露「这个 id 确实存在」这一信息。
"""

from __future__ import annotations

from typing import Any

from backend.repositories.solution_repo import SolutionRepository

# 标题长度上限。超长标题会把侧边栏撑坏，且它只是展示用的冗余值，
# 截断没有信息损失（正文在 data 里完整保留）。
_TITLE_MAX = 200
# 入口模式与阶段的长度上限：取值来自前端的联合类型，正常远小于此，
# 这里只是防止异常输入写进库里。
_MODE_MAX = 32
_STAGE_MAX = 64


def _clean(text: str, limit: int) -> str:
    """去掉首尾空白并截断到上限，避免把超长或带换行的值写进摘要列。"""
    return text.strip()[:limit]


def create_solution(owner_id: int, title: str, mode: str, stage: str, data: str) -> dict[str, Any]:
    """新建方案，返回其摘要（不含 data）。

    `data` 原样存取，**不做 JSON 解析**：快照结构属于前端契约，后端解析它就意味着
    前端每加一个字段后端都要跟着改，得不偿失。
    """
    solution_id = SolutionRepository.create(
        owner_id=owner_id,
        title=_clean(title, _TITLE_MAX),
        mode=_clean(mode, _MODE_MAX),
        stage=_clean(stage, _STAGE_MAX),
        data=data,
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
    data: str,
) -> dict[str, Any] | None:
    """整份覆盖某方案；不存在或不属于当前用户时返回 None（由路由层翻成 404）。"""
    ok = SolutionRepository.update(
        owner_id=owner_id,
        solution_id=solution_id,
        title=_clean(title, _TITLE_MAX),
        mode=_clean(mode, _MODE_MAX),
        stage=_clean(stage, _STAGE_MAX),
        data=data,
    )
    if not ok:
        return None
    return get_solution(owner_id, solution_id)


def get_solution(owner_id: int, solution_id: int) -> dict[str, Any] | None:
    """读取方案详情（含 data）；不存在或不属于当前用户时返回 None。"""
    row = SolutionRepository.get(owner_id=owner_id, solution_id=solution_id)
    return dict(row) if row else None


def list_solutions(owner_id: int) -> list[dict[str, Any]]:
    """列出当前用户的方案摘要，最近更新在前。

    走 Repository 的摘要查询，**不读 data 列**。
    """
    return [_to_summary(row) for row in SolutionRepository.list_by_owner(owner_id)]


def delete_solution(owner_id: int, solution_id: int) -> bool:
    """删除方案；不存在或不属于当前用户时返回 False。"""
    return SolutionRepository.delete(owner_id=owner_id, solution_id=solution_id)


def _to_summary(row: dict[str, Any]) -> dict[str, Any]:
    """把库行裁剪成列表 / 详情共用的摘要字段，顺带丢掉 data 以免误传给列表接口。

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
    }
