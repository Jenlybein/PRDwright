"""文档（documents）路由：读取某方案下的产物正文、提交用户手改。

**越权一律 404**（与方案路由同一条约定）：归属条件已经写在仓储层的 SQL 里，这里只把
「不属于当前用户 / 不存在」统一翻译成 404，不区分两者——区分了等于告诉请求方
「这个 id 确实存在，只是不属于你」。

产物类型 `kind` 用 `Literal` 声明：它是**闭集合**，非法取值由 FastAPI 直接判 422，
不必在服务层再手写一遍校验（多写一遍就多一处会忘记同步的地方）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.api import schemas
from backend.api.deps import acting_user
from backend.core.contracts import DocumentKind
from backend.core.errors import AppError, ErrorCode
from backend.services import document_store, solution_service
from backend.services.auth_service import User

router = APIRouter(prefix="/api/solutions/{solution_id}/documents", tags=["documents"])

# 方案不存在、或存在但不属于当前用户，都回这一句（与方案路由保持一致）
_NOT_FOUND_DETAIL = "方案不存在"


@router.get("", response_model=schemas.DocumentListResponse, summary="读取方案下的文档")
async def list_documents(
    solution_id: int,
    user: User = Depends(acting_user),
) -> schemas.DocumentListResponse:
    """读取某方案下的全部文档（至多三份）；方案不存在或非本人返回 404。

    「方案不可见」与「方案还没有文档」必须分开：前者 404，后者 200 + 空列表。
    把前者也返回空列表会掩盖归属判断写错的问题——调用方看不出区别。
    """
    if not solution_service.solution_exists(user.id, solution_id):
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)

    rows = document_store.list_documents(user.id, solution_id)
    return schemas.DocumentListResponse(
        documents=[schemas.DocumentItem(**row) for row in rows]
    )


@router.put("/{kind}", response_model=schemas.DocumentItem, summary="提交用户手改的正文")
async def save_document(
    solution_id: int,
    kind: DocumentKind,
    payload: schemas.DocumentSaveRequest,
    user: User = Depends(acting_user),
) -> schemas.DocumentItem:
    """保存用户编辑后的正文；方案不存在或非本人返回 404。

    只覆盖正文——待确认清单与用量回执是「上一次生成」的事实，不因用户改几个字而改变。
    """
    row = document_store.save_document(user.id, solution_id, kind, payload.content)
    if row is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    return schemas.DocumentItem(**row)
