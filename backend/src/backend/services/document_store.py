"""文档（documents）业务服务。

三份产物正文（PRD / 接口文档 / 提示词）在后端的一等实体。归属仍由 `solutions` 决定，
这里只做「按方案取文档」与「写入文档」两件事——不做权限判断（仓储层已经保证），
也不管 HTTP（路由层负责把 `None` 翻成 404）。

**两条写入路径的语义不同，不要混用**：

- `save_document`：用户手改正文。只动正文；`pending_items` 与 `last_run_summary`
  原样保留。待确认清单是模型产出的元数据，用户改几个字不构成「清单过期」。
- `commit_generated`：一次生成或改写收尾时落稿。正文、待确认清单、用量回执
  **三者同批替换**——它们来自同一次模型调用栈，分开写就会各自漂移。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.core.digest import content_fingerprint
from backend.repositories.document_repo import DocumentRepository

logger = logging.getLogger(__name__)


def list_documents(owner_id: int, solution_id: int) -> list[dict[str, Any]]:
    """列出某方案下的全部文档；方案不存在或不属于该用户时返回空列表。"""
    return [
        _to_item(row)
        for row in DocumentRepository.list_by_solution(owner_id, solution_id)
    ]


def get_document(owner_id: int, solution_id: int, kind: str) -> dict[str, Any] | None:
    """读取一份文档；不存在或不属于该用户时返回 None（由路由层翻成 404）。"""
    row = DocumentRepository.get(owner_id, solution_id, kind)
    return _to_item(row) if row else None


def save_document(
    owner_id: int, solution_id: int, kind: str, content: str
) -> dict[str, Any] | None:
    """保存用户手改后的正文，返回更新后的文档；方案不可见时返回 None。"""
    ok = DocumentRepository.save_content(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        content=content,
        content_sha=content_fingerprint(content),
    )
    if not ok:
        return None
    return get_document(owner_id, solution_id, kind)


def commit_generated(
    owner_id: int,
    solution_id: int,
    kind: str,
    content: str,
    *,
    pending_items: list[dict[str, Any]] | None = None,
    run_summary: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """生成（或改写）收尾落稿：正文 + 待确认清单 + 用量回执一同写入。

    Args:
        owner_id: 文档所属用户。
        solution_id: 文档所属方案。
        kind: 产物类型（`prd` / `api_docs` / `prompts`）。
        content: 已剥离标记块的正文。
        pending_items: 本次产出的待确认清单；`None` 视为空清单。
        run_summary: 本次生成的用量回执；`None` 表示不写（保持原值）。

    Returns:
        更新后的文档；方案不可见时返回 None。
    """
    ok = DocumentRepository.commit_generated(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        content=content,
        content_sha=content_fingerprint(content),
        pending_items=json.dumps(pending_items or [], ensure_ascii=False),
        last_run_summary=(
            json.dumps(run_summary, ensure_ascii=False) if run_summary is not None else None
        ),
    )
    if not ok:
        return None
    return get_document(owner_id, solution_id, kind)


def _to_item(row: dict[str, Any]) -> dict[str, Any]:
    """把库行裁剪成对外结构，并把两个 JSON 文本列解成对象。

    解析一律容错（脏数据退化为空清单 / 空回执）：这两个列是**展示用**的附加信息，
    读不动它不该让整份文档读不出来——正文才是主角。
    """
    return {
        "kind": row["kind"],
        "content": row["content"],
        "pending_items": _load_json(row["pending_items"], list, []),
        "last_run_summary": _load_json(row["last_run_summary"], dict, None),
        "updated_at": row["updated_at"],
    }


def _load_json(raw: Any, expected: type, fallback: Any) -> Any:
    """把 JSON 文本解析成 `expected` 类型；任何异常都退化为 `fallback`。"""
    if not isinstance(raw, str) or not raw.strip():
        return fallback
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("文档字段不是合法 JSON，已按空值处理", extra={"event": "document.bad_json"})
        return fallback
    return parsed if isinstance(parsed, expected) else fallback
