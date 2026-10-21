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

    # ------------------------------------------------------------------
    # 检索（v4 T4：把攒下来的日志变成可查的）
    # ------------------------------------------------------------------

    @staticmethod
    def search(
        *,
        level: str | None = None,
        since: str | None = None,
        until: str | None = None,
        logger_name: str | None = None,
        event: str | None = None,
        request_id: str | None = None,
        user_id: int | None = None,
        keyword: str | None = None,
        exclude_event: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """按条件检索日志，返回 `(当页行, 命中总数)`。

        为什么返回总数
        --------------
        分页器必须知道「一共多少条」才能显示页数；让前端自己推算会不准
        （它只知道自己拿到的那一页）。两条 SQL 在**同一个连接**里跑，
        保证「总数」与「当页」看到的是同一时刻的表状态。

        过滤条件的取舍
        --------------
        - **级别用 `>=` 而不是等值**：选 `WARNING` 时人想看的是「警告及以上」，
          而不是「只要 WARNING、不要 ERROR」——后者几乎没人需要，却更容易误解。
          级别在 SQL 里按 `level_rank` 映射成数字比较（SQLite 无枚举类型）。
        - **关键词只搜 `msg`**：`extra_json` 也被搜的话，一次 `LIKE` 会扫全表两列，
          在 SQLite 上代价明显；而人找日志时最先记得的是那句话本身。
          要按结构化字段找，用 `event` / `request_id` / `user_id` 那几个精确条件。
        - **`logger` 用前缀匹配**（`LIKE 'x%'`）：`backend.services.job_service` 这整棵
          子树一起看是常态，要求精确等于反而更麻烦。
        - 所有条件都是**可选**：不传就不加那个 `WHERE` 子句，而不是传空串当条件。

        Args:
            level: 最低级别（INFO / WARNING / ERROR / CRITICAL）。
            since: 起始时间（含），`utc_ms_text` 格式。
            until: 结束时间（含），同格式。
            logger_name: logger 名前缀。
            event: 事件名，精确匹配。
            request_id: 请求号，精确匹配（排障主路径）。
            user_id: 归属用户。
            keyword: 消息关键词（`LIKE %kw%`）。
            exclude_event: 要**排除**的事件名。访问日志（`request.completed`）每来一个
                请求就写一条，量级远远压过业务事件——日志页默认把它排掉，
                否则「刚才那次生成怎么了」会被几十条「请求完成」埋掉。
            limit: 每页条数。
            offset: 跳过多少条。

        Returns:
            `(rows, total)`：`rows` 为当前页（按时间倒序），`total` 为命中总数。
        """
        # 级别映射：SQLite 里没有枚举，靠 `CASE` 把字符串折成可比较的序。
        # 未登记的级别一律当 0（低于 INFO），于是「按 INFO 过滤」不会把它漏掉。
        level_rank = (
            "CASE level WHEN 'DEBUG' THEN 10 WHEN 'INFO' THEN 20 "
            "WHEN 'WARNING' THEN 30 WHEN 'ERROR' THEN 40 WHEN 'CRITICAL' THEN 50 "
            "ELSE 0 END"
        )

        clauses: list[str] = []
        params: list[Any] = []

        if level:
            min_rank = {
                "DEBUG": 10,
                "INFO": 20,
                "WARNING": 30,
                "ERROR": 40,
                "CRITICAL": 50,
            }.get(level.upper())
            # 认不出的级别就不过滤，而不是返回空列表：
            # 「筛了个不存在的级别」应当表现为「没筛」，让调用方看到数据而非空页。
            if min_rank is not None:
                clauses.append(f"{level_rank} >= ?")
                params.append(min_rank)

        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if until:
            clauses.append("ts <= ?")
            params.append(until)
        if logger_name:
            # 前缀匹配：给 `LIKE` 的通配符只有尾部一个，仍可走索引
            clauses.append("logger LIKE ?")
            params.append(f"{logger_name}%")
        if event:
            clauses.append("event = ?")
            params.append(event)
        if request_id:
            clauses.append("request_id = ?")
            params.append(request_id)
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if exclude_event:
            # 用 `IS NOT` 而不是 `!=`：`event` 可为 NULL，而 `NULL != 'x'`
            # 在 SQL 里求值为 NULL（不成立），会把所有「没有事件名」的日志一起丢掉。
            # `IS NOT` 对 NULL 成立，语义正是「不是这个事件（含没有事件名的）」。
            clauses.append("event IS NOT ?")
            params.append(exclude_event)
        if keyword:
            # 转义 `%` / `_`：用户搜「50%」时不该被当成通配符，否则会搜出无关的行
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("msg LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS n FROM app_logs {where}", tuple(params)
            ).fetchone()
            total = int(total_row["n"]) if total_row else 0

            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM app_logs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(int(limit), 500)), max(0, int(offset))),
            ).fetchall()
            return [dict(row) for row in rows], total
