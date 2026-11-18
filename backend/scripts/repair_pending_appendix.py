"""一次性修整（不进版本库，不随镜像分发）：把存量正文里的人读附录剥离并补上清单。

背景：待确认清单的抽取发生在**生成时**。2026-11-18 修好的兜底抽取只对**新生成**的
文档生效；已经落库的文档正文里仍然留着「## 附：待确认问题」那段附录，`pending_items`
也是空的——界面既看不到容器，正文又挂着一段元数据。

做法：走仓库自己的写入入口 `document_store.write_document(mode="replace")`（就地改写
当前版本，不追加版本、不动版本链），`source="migrated"` 标明这是数据修整而不是用户手改。
幂等：正文里已经没有附录可剥时就跳过。

用法（在 api 容器内跑，连的是同一个库）：
    docker exec prdwright-api-1 python /tmp/repair_pending_appendix.py --dry-run
    docker exec prdwright-api-1 python /tmp/repair_pending_appendix.py --apply
"""

from __future__ import annotations

import argparse
import sys

from backend.core.engine import session_scope
from backend.repositories.document_repo import DocumentRepository
from backend.services import document_store
from backend.services.pending_items import extract


def _all_solutions_documents() -> list[dict]:
    """取出全部方案的当前文档（含正文）——修整脚本才需要的宽查询。"""
    from backend.core.models import Document, DocumentVersion, Solution
    from sqlalchemy import select

    rows: list[dict] = []
    with session_scope() as session:
        owner_by_solution = {
            int(sol.id): int(sol.user_id) for sol in session.scalars(select(Solution)).all()
        }
        docs = session.scalars(select(Document)).all()
        for doc in docs:
            if not doc.current_version_id:
                continue
            version = session.get(DocumentVersion, int(doc.current_version_id))
            if version is None:
                continue
            owner_id = owner_by_solution.get(int(doc.solution_id))
            if owner_id is None:
                continue
            rows.append(
                {
                    "owner_id": owner_id,
                    "solution_id": int(doc.solution_id),
                    "kind": str(doc.kind),
                    "content": str(version.content or ""),
                    "version_no": int(version.version_no or 0),
                    "pending_items": version.pending_items or "",
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真的写库（缺省只预览）")
    args = parser.parse_args()

    repaired = 0
    skipped = 0
    for row in _all_solutions_documents():
        content = row["content"]
        clean, items = extract(content)
        if not items or clean == content:
            skipped += 1
            continue
        print(
            f"[{row['kind']}] solution={row['solution_id']} v{row['version_no']} "
            f"剥离 {len(items)} 条待确认，正文 {len(content)} → {len(clean)} 字节"
        )
        if not args.apply:
            for item in items[:3]:
                print(f"    · {item['importance']} {item['question'][:40]}")
            continue
        document_store.write_document(
            row["owner_id"],
            row["solution_id"],
            row["kind"],
            clean,
            mode="replace",
            expected_version_no=row["version_no"],
            pending_items=items,
            source="migrated",
            note="修整：剥离正文里的人读待确认附录并补登记清单",
        )
        repaired += 1

    print(f"完成：修整 {repaired} 份，跳过 {skipped} 份")
    return 0


if __name__ == "__main__":
    sys.exit(main())
