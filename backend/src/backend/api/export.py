"""交付包导出：把三份产物与一份 README 打成一个 ZIP。

为什么是「三份 md + README」而不是「三份 md + manifest.json」：
- 交付物是给人看的，`README.md` 能直接说明阅读顺序与缺失项；机器可读的 manifest
  当前没有消费方（质量报告已决定不做、审核结论不持久化），留着只会是一个没人读的文件。
- 版本号/指纹仍在各版本行的 `metadata` 里，需要排查时从接口取，不必塞进压缩包。

越权与不存在一律 404（与其它方案级路由同一口径）；三份都为空时 400：
导出不是「完整性门禁」，但全空时导出一个只有 README 的包没有意义。
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Response

from backend.api.deps import acting_user
from backend.core.errors import AppError, ErrorCode
from backend.services import document_store, solution_service
from backend.services.auth_service import User

router = APIRouter(prefix="/api/solutions/{solution_id}/export", tags=["export"])

_NOT_FOUND_DETAIL = "方案不存在"
_EMPTY_DETAIL = "暂无可交付的文档"

# 产物 → 压缩包内文件名。用 ASCII 文件名避免解压时的编码差异；
# 中文说明由 README 承担。
_DOC_FILES: dict[str, str] = {
    "prd": "PRD.md",
    "api_docs": "API-Docs.md",
    "prompts": "Prompts.md",
}

_DOC_TITLES: dict[str, str] = {
    "prd": "PRD",
    "api_docs": "接口文档",
    "prompts": "提示词套件",
}


@router.get("", summary="导出交付包（ZIP）")
async def export_delivery(
    solution_id: int,
    user: User = Depends(acting_user),
) -> Response:
    """导出方案下的三份产物 + README；方案不可见 404，全空 400。"""
    if not solution_service.solution_exists(user.id, solution_id):
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)

    docs = document_store.list_documents(user.id, solution_id)
    by_kind = {doc["kind"]: doc for doc in docs}
    if not any((doc.get("content") or "").strip() for doc in docs):
        raise AppError(ErrorCode.INVALID_REQUEST, _EMPTY_DETAIL)

    solution = solution_service.get_solution(user.id, solution_id) or {}
    product_name = str(solution.get("title") or "").strip() or "未命名产品"
    readme = _build_readme(product_name, by_kind)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.md", readme)
        for kind, filename in _DOC_FILES.items():
            doc = by_kind.get(kind)
            if doc and (doc.get("content") or "").strip():
                archive.writestr(filename, doc["content"])

    zip_name = f"{product_name}-delivery.zip"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(zip_name, safe='')}"
    }
    return Response(content=buffer.getvalue(), media_type="application/zip", headers=headers)


def _build_readme(product_name: str, by_kind: dict[str, dict]) -> str:
    """生成给人看的交付说明：阅读顺序、每份的状态、以及“基于旧版”的提示。

    README 里**不写审核结论**：那是会话内的临时信息，不落库；写进来会变成
    “导出时看起来审过了、下次打开却没有”的第二个真源。
    """
    exported_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [
        f"# {product_name} 交付说明",
        "",
        f"导出时间：{exported_at}",
        "",
        "## 阅读顺序",
        "",
        "1. `PRD.md` —— 需求、功能范围与验收标准",
        "2. `API-Docs.md` —— 前后端接口约定",
        "3. `Prompts.md` —— 交给 AI 编码工具的分步提示词",
        "",
        "## 文档清单",
        "",
    ]
    for kind, title in _DOC_TITLES.items():
        doc = by_kind.get(kind)
        if doc and (doc.get("content") or "").strip():
            note = "；基于旧版上游，可能已过期" if doc.get("stale") else ""
            lines.append(
                f"- {title}：已生成（{len(doc['content'])} 字符，"
                f"更新于 {doc.get('updated_at', '')}）{note}"
            )
        else:
            lines.append(f"- {title}：未生成")
    lines += ["", "---", "", "由 PRDwright 导出。"]
    return "\n".join(lines) + "\n"
