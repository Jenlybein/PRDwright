"""文档（documents）路由：读取某方案下的产物正文、提交用户手改、管理文档版本。

**越权一律 404**（与方案路由同一条约定）：归属条件已经写在仓储层的 SQL 里，这里只把
「不属于当前用户 / 不存在」统一翻译成 404，不区分两者——区分了等于告诉请求方
「这个 id 确实存在，只是不属于你」。

产物类型 `kind` 用 `Literal` 声明：它是**闭集合**，非法取值由 FastAPI 直接判 422，
不必在服务层再手写一遍校验（多写一遍就多一处会忘记同步的地方）。

版本路由刻意挂在 **solution 作用域**下（`/api/solutions/{id}/documents/{kind}/...`），
而不是 `/api/documents/{kind}/...`：归属只能经 `documents → solutions` 两级 JOIN 过滤，
作用域带上方案 id 才能在 SQL 层一次判完，守住「越权不可区分」的口径。

版本不存在**复用** `SOLUTION_NOT_FOUND`（HTTP 404）：错误码表刻意不膨胀
（ADR-012），前端只按 404 状态分支，不拿中文 detail 判断。
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
# 版本不存在、或不属于该用户，回这一句；与上面的码相同、只是展示文案不同
_VERSION_NOT_FOUND_DETAIL = "版本不存在"


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


# ---------------------------------------------------------------------------
# 版本链：存为新版本 / 看历史 / 恢复
# ---------------------------------------------------------------------------


@router.get(
    "/{kind}/versions",
    response_model=schemas.DocumentVersionListResponse,
    summary="读取某份文档的版本列表",
)
async def list_versions(
    solution_id: int,
    kind: DocumentKind,
    user: User = Depends(acting_user),
) -> schemas.DocumentVersionListResponse:
    """读取某份文档的版本摘要（不含正文），从新到旧；方案不可见返回 404。

    「方案不可见」与「还没有任何版本」必须分开：前者 404，后者 200 + 空列表——
    把前者也返回空列表会掩盖归属判断写错的问题。
    """
    if not solution_service.solution_exists(user.id, solution_id):
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)

    rows = document_store.list_versions(user.id, solution_id, kind)
    return schemas.DocumentVersionListResponse(
        versions=[schemas.DocumentVersionItem(**row) for row in rows]
    )


@router.get(
    "/{kind}/versions/{version_id}",
    response_model=schemas.DocumentVersionDetail,
    summary="读取某个历史版本",
)
async def get_version(
    solution_id: int,
    kind: DocumentKind,
    version_id: int,
    user: User = Depends(acting_user),
) -> schemas.DocumentVersionDetail:
    """读取一条版本的完整内容（含正文 / 清单 / 回执）；方案或版本不可见返回 404。"""
    if not solution_service.solution_exists(user.id, solution_id):
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)

    row = document_store.get_version(user.id, solution_id, kind, version_id)
    if row is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _VERSION_NOT_FOUND_DETAIL)
    return schemas.DocumentVersionDetail(**row)


@router.post(
    "/{kind}/checkpoint",
    response_model=schemas.DocumentItem,
    summary="把当前正文存为一个新版本",
)
async def checkpoint(
    solution_id: int,
    kind: DocumentKind,
    payload: schemas.DocumentCheckpointRequest,
    user: User = Depends(acting_user),
) -> schemas.DocumentItem:
    """把用户眼前的正文追加为一个新版本（打里程碑），返回新的当前文档。

    **不是**「写正文」的通道：它只把当前内容留档，正文的权威仍是手改 / 生成两条路径。
    """
    row = document_store.checkpoint(user.id, solution_id, kind, payload.content, payload.note)
    if row is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    return schemas.DocumentItem(**row)


@router.post(
    "/{kind}/restore",
    response_model=schemas.DocumentItem,
    summary="恢复某个历史版本",
)
async def restore(
    solution_id: int,
    kind: DocumentKind,
    payload: schemas.DocumentRestoreRequest,
    user: User = Depends(acting_user),
) -> schemas.DocumentItem:
    """把某个历史版本恢复为当前版本（实现为「追加一条新版本」），返回新的当前文档。

    历史只增不改：恢复本身也留一条 `source='restore'` 的版本，因此「恢复」这个动作可回溯。
    """
    if not solution_service.solution_exists(user.id, solution_id):
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)

    row = document_store.restore(user.id, solution_id, kind, payload.version_id)
    if row is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _VERSION_NOT_FOUND_DETAIL)
    return schemas.DocumentItem(**row)
