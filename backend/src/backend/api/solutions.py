"""方案（solutions）路由：列表 / 创建 / 读取 / 更新 / 删除。

本层只做参数解析、归属校验的**结果翻译**与响应封装，业务判断在
`services/solution_service.py`，数据隔离在 `repositories/solution_repo.py`。

**越权一律返回 404 而非 403。** 用 403 等于告诉请求方「这个 id 确实存在，只是不属于你」，
攻击者可以借此枚举出系统里有哪些方案；统一 404 则不泄露任何存在性信息。
Repository 已经保证「不属于你的」在 SQL 层面就查不到，这里只是把 None 翻译成 404。

当前用户经 `acting_user` 获取：`AUTH_ENABLED=false` 的开发模式下它会回落到内置 dev 用户，
因此 `user_id` 恒非空，数据模型不必为开发模式开一个 NULL 分支。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from backend.api import schemas
from backend.api.deps import acting_user
from backend.core.errors import AppError, ErrorCode
from backend.services import solution_service
from backend.services.auth_service import User

router = APIRouter(prefix="/api/solutions", tags=["solutions"])

# 方案不存在、或存在但不属于当前用户，都回这一句。
_NOT_FOUND_DETAIL = "方案不存在"


@router.get("", response_model=schemas.SolutionListResponse, summary="列出我的方案")
async def list_solutions(user: User = Depends(acting_user)) -> schemas.SolutionListResponse:
    """列出当前用户的方案摘要，最近更新在前。不返回快照正文。"""
    rows = solution_service.list_solutions(user.id)
    return schemas.SolutionListResponse(
        solutions=[schemas.SolutionSummary(**row) for row in rows]
    )


@router.post(
    "",
    response_model=schemas.SolutionSummary,
    status_code=status.HTTP_201_CREATED,
    summary="创建方案",
)
async def create_solution(
    payload: schemas.SolutionSaveRequest,
    user: User = Depends(acting_user),
) -> schemas.SolutionSummary:
    """创建一份方案。

    前端在快照**首次出现非空内容**时调用；此后同一份方案走 `PUT` 更新。
    """
    row = solution_service.create_solution(
        owner_id=user.id,
        title=payload.title,
        mode=payload.mode,
        stage=payload.stage,
        data=payload.data,
    )
    return schemas.SolutionSummary(**row)


@router.get("/{solution_id}", response_model=schemas.SolutionDetail, summary="读取方案详情")
async def get_solution(
    solution_id: int,
    user: User = Depends(acting_user),
) -> schemas.SolutionDetail:
    """读取方案详情（含全量快照文本）；不存在或非本人返回 404。"""
    row = solution_service.get_solution(user.id, solution_id)
    if row is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    return schemas.SolutionDetail(**row)


@router.put("/{solution_id}", response_model=schemas.SolutionDetail, summary="更新方案")
async def update_solution(
    solution_id: int,
    payload: schemas.SolutionSaveRequest,
    user: User = Depends(acting_user),
) -> schemas.SolutionDetail:
    """整份覆盖方案；不存在或非本人返回 404。

    刻意不做「字段级局部更新」：快照是**整份**状态，局部更新会引入「两次保存交错时
    谁的字段生效」这类难以推理的问题，而整份覆盖的语义是确定且幂等的。
    """
    row = solution_service.update_solution(
        owner_id=user.id,
        solution_id=solution_id,
        title=payload.title,
        mode=payload.mode,
        stage=payload.stage,
        data=payload.data,
    )
    if row is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    return schemas.SolutionDetail(**row)


@router.delete(
    "/{solution_id}",
    response_model=schemas.SolutionDeleteResponse,
    summary="删除方案",
)
async def delete_solution(
    solution_id: int,
    user: User = Depends(acting_user),
) -> schemas.SolutionDeleteResponse:
    """删除方案；不存在或非本人返回 404。

    同样返回 404 而不是「静默成功」：否则删除别人的方案会与删除自己的返回相同结果，
    调用方无法察觉自己的归属判断写错了。
    """
    if not solution_service.delete_solution(user.id, solution_id):
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    return schemas.SolutionDeleteResponse(ok=True)
