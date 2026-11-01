"""按天统计的时区边界（v8 / D5）。

旧实现让数据库做换算（`datetime(created_at, '+480 minutes')`），改成应用层算边界之后，
最容易错的不是聚合口径，是**边界本身**。这组用例钉两条：

1. **左闭右开**：本地零点整属于当天，零点前 1 微秒属于前一天，不能两边都算、也不能
   两边都不算；
2. **边界的字符串形态**：库里存的是 `...T12:34:56.789012`（可能带微秒，也可能不带），
   而边界由 `isoformat()` 生成——微秒为 0 时它会**省略**那一段。若边界写成
   `...T16:00:00.000000`，字典序会让库里整点的旧值 `...T16:00:00`（更短、更小）
   落到区间之外，少算一条且不报错。

为什么不复用 `test_admin_stats.py` 的造数函数：那个文件的用例围绕「口径」
（取消算不算失败、缺日补 0、token 是否累加），这里围绕「边界算术」。共用一个
造数函数会让改一边时另一边跟着动；造数只有十行，复制比耦合便宜。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from conftest import register, users

from backend.core.clock import utc_now
from dbsupport import connect


def _as_admin(client, username: str = "root") -> None:
    """注册并任命一个管理员（顺带让 client 处于该账号的登录态）。"""
    from backend.services import auth_service

    register(client, username)
    auth_service.set_admin(username, True)


def _user_id(username: str) -> int:
    from backend.services import auth_service

    row = users.get_by_username(username)
    assert row is not None
    return int(row["id"])


def _job_at(owner_id: int, moment: datetime) -> None:
    """在指定时刻插一条已完成任务（绕开仓储的 running-only 建法）。"""
    text = moment.isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT INTO generation_jobs "
            "(owner_id, solution_id, kind, doc_kind, chat_stage, status, phase, "
            " draft_content, completed_sections, pending_items, input_fingerprint, usage, "
            " max_provider_switches, error_code, error_message, "
            " provider_protocol, provider_config_id, provider_name, provider_model, "
            " prompt_tokens, completion_tokens, created_at, updated_at) "
            "VALUES (?, NULL, 'prd', '', '', 'completed', '', '', '[]', '[]', NULL, NULL, 2, '', '', '', '', '', '', 0, 0, ?, ?)",
            (owner_id, text, text),
        )
        conn.commit()


def _local_day(offset_minutes: int) -> date:
    """按给定偏移的「本地今天」。"""
    return (utc_now() + timedelta(minutes=offset_minutes)).date()


def _local_midnight_utc(day: date, offset_minutes: int) -> datetime:
    """某个本地日零点对应的 UTC 时刻（naive）。"""
    return datetime.combine(day, time.min) - timedelta(minutes=offset_minutes)


def _counts(client, *, days: int, offset: int) -> dict[str, int]:
    """取「日期 → 任务数」，顺带断言响应形状。"""
    response = client.get(
        "/api/admin/stats/daily", params={"days": days, "tz_offset_minutes": offset}
    )
    assert response.status_code == 200, response.text
    return {item["day"]: item["task_count"] for item in response.json()["items"]}


# ---------------------------------------------------------------------------
# 边界：左闭右开
# ---------------------------------------------------------------------------


def test_local_midnight_counts_in_that_day(client):
    """本地零点整（无微秒形态）必须归当天。

    这条同时覆盖了「库里存的是不带微秒的整点值」——正是字典序陷阱最容易被咬到的形态。
    """
    offset = 480
    _as_admin(client)
    owner = _user_id("root")
    today = _local_day(offset)
    midnight = _local_midnight_utc(today, offset)

    _job_at(owner, midnight)  # 本地今天 00:00:00
    _job_at(owner, midnight + timedelta(microseconds=1))  # 本地今天 00:00:00.000001

    counts = _counts(client, days=2, offset=offset)
    assert counts[today.isoformat()] == 2
    assert counts[(today - timedelta(days=1)).isoformat()] == 0


def test_microsecond_before_midnight_belongs_to_previous_day(client):
    """本地零点前 1 微秒必须归前一天——区间不能两边都包。"""
    offset = 480
    _as_admin(client)
    owner = _user_id("root")
    today = _local_day(offset)
    midnight = _local_midnight_utc(today, offset)

    _job_at(owner, midnight - timedelta(microseconds=1))

    counts = _counts(client, days=2, offset=offset)
    assert counts[(today - timedelta(days=1)).isoformat()] == 1
    assert counts[today.isoformat()] == 0


# ---------------------------------------------------------------------------
# 偏移不是整小时
# ---------------------------------------------------------------------------


def test_half_hour_offset_splits_at_correct_midnight(client):
    """+5:30 这种半小时偏移下，边界必须落在「本地零点」，不是整点。

    旧实现用 `datetime(col, '+330 minutes')` 也能算对；但「按整小时分组再在应用层
    合并」的方案会在这里错——整小时桶会横跨两个本地日。这条例钉的就是那条被否决的
    思路，以及新实现的精确性。
    """
    offset = 330
    _as_admin(client)
    owner = _user_id("root")
    today = _local_day(offset)
    midnight = _local_midnight_utc(today, offset)

    _job_at(owner, midnight - timedelta(microseconds=1))  # 本地前一天 23:59:59.999999
    _job_at(owner, midnight)  # 本地当天 00:00:00

    counts = _counts(client, days=2, offset=offset)
    assert counts[(today - timedelta(days=1)).isoformat()] == 1
    assert counts[today.isoformat()] == 1


def test_negative_offset_splits_at_correct_midnight(client):
    """负偏移（-5:00）同样要按本地零点切。"""
    offset = -300
    _as_admin(client)
    owner = _user_id("root")
    today = _local_day(offset)
    midnight = _local_midnight_utc(today, offset)

    _job_at(owner, midnight - timedelta(seconds=1))
    _job_at(owner, midnight)

    counts = _counts(client, days=2, offset=offset)
    assert counts[(today - timedelta(days=1)).isoformat()] == 1
    assert counts[today.isoformat()] == 1


# ---------------------------------------------------------------------------
# 窗口本身
# ---------------------------------------------------------------------------


def test_window_length_matches_requested_days(client):
    """`days` 决定行数，且日期连续升序、最后一个是本地今天。

    这条与口径无关，守的是「补 0 的窗口」没被边界改动带偏。
    """
    offset = 480
    _as_admin(client)

    response = client.get(
        "/api/admin/stats/daily", params={"days": 5, "tz_offset_minutes": offset}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    days = [item["day"] for item in body["items"]]
    assert body["days"] == 5
    assert len(days) == 5
    assert days == sorted(days)
    assert days[-1] == _local_day(offset).isoformat()
    assert all(item["task_count"] == 0 for item in body["items"])
