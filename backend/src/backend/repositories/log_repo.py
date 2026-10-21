"""应用日志的数据仓储：`app_logs` 表。

为什么单独一个仓储
------------------
这张表的写入方不是常规请求链路，而是 `core/logging_setup.py` 挂的 `LogStoreHandler`
（跑在后台线程里）。仍然把它收进 `repositories/`，是为了守住「SQL 只出现在
repositories/」这条约束——分层纪律不该因为「这次的调用方长得不一样」就破例。

它的调用频率远高于其他仓储，所以这里**只暴露批量与裁剪两个动作**：
单条插入在此是反模式（每条一个事务会把日志写放大成 DB 写压力）。

本表是**可丢弃的派生数据**：清空它不影响任何业务，也不参与任何外键。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from backend.core.db import connect, utc_ms_text, utc_now

# 检索时显式列清单，不用 `SELECT *`：将来给这张表加列时，
# 不该静默改变接口的返回结构（T4 的检索接口直接复用它）。
_COLUMNS = "id, ts, level, logger, msg, request_id, user_id, event, extra_json, exc"


class LogRepository:
    """`app_logs` 表的读写。"""

    @staticmethod
    def insert_many(rows: list[dict[str, Any]]) -> int:
        """批量写入日志行，返回实际写入的条数。

        空列表直接返回：批量插入空序列虽然无害，但会让「写了几批」这个统计
        凭空多出一堆空事务，排查时反而误导。

        整批在一个事务里：日志宁可整批丢，也不该出现「写了一半」的中间态——
        半批日志会让「这次请求到底跑到哪一步」变得比没有日志更难判断。
        """
        if not rows:
            return 0
        payload = [
            (
                row["ts"],
                row["level"],
                row["logger"],
                row["msg"],
                row["request_id"],
                row["user_id"],
                row["event"],
                row["extra_json"],
                row["exc"],
            )
            for row in rows
        ]
        with connect() as conn:
            conn.executemany(
                "INSERT INTO app_logs "
                "(ts, level, logger, msg, request_id, user_id, event, extra_json, exc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            conn.commit()
        return len(payload)

    @staticmethod
    def prune(*, retention_days: int, max_rows: int) -> int:
        """按「保留天数」与「行数上限」两个闸门裁剪，返回删除的总行数。

        两个闸门**谁先触发谁生效**，不是「同时满足才删」：天数防的是「太久远的日志
        还占着」，行数防的是「短时间日志爆炸把库撑大」，两者作用在不同失效模式上。

        裁剪靠**字符串比较时间**（`ts < ?`）——这正是 `ts` 必须与日志格式化器
        用同一格式（`utc_ms_text`）的原因，否则字典序会悄悄错位。
        """
        cutoff = utc_ms_text(utc_now() - timedelta(days=retention_days))
        with connect() as conn:
            cursor = conn.execute("DELETE FROM app_logs WHERE ts < ?", (cutoff,))
            deleted = int(cursor.rowcount or 0)

            # 行数闸门：保住最新的 `max_rows` 条，其余按 id 一次性删掉。
            # 子查询取「第 max_rows+1 新的那条的 id」；行数没超限时它返回 NULL，
            # `id <= NULL` 恒不成立 → 一条都不删。这样不必先 count 再判断，
            # 少一次往返，也少一处「判断与删除之间又插进新日志」的竞态。
            cursor = conn.execute(
                "DELETE FROM app_logs WHERE id <= ("
                "SELECT id FROM app_logs ORDER BY id DESC LIMIT 1 OFFSET ?"
                ")",
                (max_rows,),
            )
            deleted += int(cursor.rowcount or 0)
            conn.commit()
        return deleted

    @staticmethod
    def count() -> int:
        """表内行数。给裁剪策略与测试用。"""
        with connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM app_logs").fetchone()
            return int(row["n"]) if row else 0
