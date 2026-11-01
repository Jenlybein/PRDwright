"""大段文本的 60KB 闸门（v8）。

口径是**字节**不是字符：中文一个字 3 字节，所以「6 万字」是 180KB。用例里专门
用汉字钉住这一点——如果哪天有人把 `len(value)` 当成字节数，只有中文本例会红。

这组用例守三件事：

1. 边界是**闭区间**：恰好 60KB 通过，多 1 字节拒绝。差一个字节的 off-by-one
   在"上限"这类规则里最容易写错，也最难在线上发现（要到某次刚好超一点才炸）；
2. 错误码复用 `INVALID_REQUEST`，不新增码——`core/errors.py` 明确要求成员数保持 9 个；
3. 文案里带**实际字节数与上限**。只说"太长了"会让人反复试，说"当前 61,443 字节，
   上限 61,440"能直接判断要删多少。
"""

from __future__ import annotations

import pytest

from backend.core.errors import AppError, ErrorCode
from backend.core.limits import MAX_TEXT_BYTES, ensure_text_within_limit, text_size


def test_max_is_60kb_in_bytes():
    """常量必须按字节、且就是 60KB——改这个数等于改产品口径，应当显式看到。"""
    assert MAX_TEXT_BYTES == 60 * 1024 == 61_440


def test_exactly_at_limit_passes():
    """恰好 60KB 通过：上限是闭区间。"""
    ensure_text_within_limit("a" * MAX_TEXT_BYTES, field="正文")


def test_one_byte_over_is_rejected():
    with pytest.raises(AppError) as caught:
        ensure_text_within_limit("a" * (MAX_TEXT_BYTES + 1), field="文档正文")

    error = caught.value
    assert error.code is ErrorCode.INVALID_REQUEST
    # 文案要能直接指导操作：说清超了多少、上限是多少
    assert "文档正文" in error.message
    assert "60KB" in error.message
    assert f"{MAX_TEXT_BYTES + 1:,}" in error.message


def test_chinese_counts_by_bytes_not_characters():
    """中文一个字 3 字节：20,480 个汉字恰好 60KB，20,481 个就超。

    这条是整组用例的核心——如果有人把上限按字符数实现，20,481 个汉字只有 20KB 出头，
    会被错误地放行，然后在 MySQL 里以 `Data too long` 的形式炸在写入那一刻。
    """
    assert text_size("中") == 3

    just_fits = "中" * 20_480
    assert text_size(just_fits) == MAX_TEXT_BYTES
    ensure_text_within_limit(just_fits, field="正文")

    one_more = "中" * 20_481
    with pytest.raises(AppError):
        ensure_text_within_limit(one_more, field="正文")


def test_none_and_empty_pass():
    """未写内容不是"超长"。`None` 会被上层折成空串，这里不该拦。"""
    ensure_text_within_limit(None, field="正文")
    ensure_text_within_limit("", field="正文")


def test_emoji_uses_four_bytes():
    """4 字节平面（emoji）也要按真实字节算，不能被 3 字节的假设漏掉。"""
    assert text_size("😀") == 4
    limit_count = MAX_TEXT_BYTES // 4
    ensure_text_within_limit("😀" * limit_count, field="正文")
    with pytest.raises(AppError):
        ensure_text_within_limit("😀" * (limit_count + 1), field="正文")


# ---------------------------------------------------------------------------
# 调用点：闸门必须真的接在仓储入口上
# ---------------------------------------------------------------------------


def test_append_version_rejects_oversized_content(client):
    """写正文的唯一入口是 `append_version`，闸门要长在这里。

    只测 `ensure_text_within_limit` 不够——那只证明函数对，不证明它被调用了。
    这条用例走完整仓储路径，并断言**库里一行都没多**：异常必须让事务整体回滚，
    而不是留下「文档行建了、版本没插」的半成品。
    """
    from conftest import register, users

    from backend.core.clock import utc_now_text
    from backend.core.engine import session_scope
    from backend.core.models import Solution
    from backend.repositories.document_repo import DocumentRepository
    from dbsupport import connect

    register(client, "lim")
    row = users.get_by_username("lim")
    assert row is not None
    owner_id = int(row["id"])

    now = utc_now_text()
    with session_scope() as session:
        solution = Solution(
            user_id=owner_id,
            title="超长用例",
            mode="prd",
            stage="draft",
            created_at=now,
            updated_at=now,
            content_updated_at=now,
        )
        session.add(solution)
        session.flush()
        solution_id = int(solution.id or 0)

    with pytest.raises(AppError) as caught:
        with session_scope() as session:
            DocumentRepository(session).append_version(
                owner_id,
                solution_id,
                "prd",
                content="中" * 20_481,  # 61,443 字节，超 3 字节
                content_sha="x",
                pending_items="[]",
                last_run_summary=None,
                source="generated",
            )
    assert caught.value.code is ErrorCode.INVALID_REQUEST

    with connect() as conn:
        versions = conn.execute("SELECT count(*) FROM document_versions").fetchone()[0]
        documents = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    assert versions == 0, "超长内容不能写进版本表"
    assert documents == 0, "事务必须整体回滚，不能留下空壳文档行"

