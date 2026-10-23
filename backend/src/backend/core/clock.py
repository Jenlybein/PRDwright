"""时间工具（v6 / ADR-077）：库里所有时间文本的唯一来源。

## 为什么单独一个模块

时间格式不是「随手 `isoformat()` 一下」，而是**跨模块的协议**：写入端与比较端必须
用同一种字符串，否则字典序 ≠ 时间序，而 `WHERE ts < ?` 这类写法**不会报错**，只会
静默算错。以前这些函数住在 `core/db.py`（那个模块还负责连接），于是「只想格式化
一个时间」也得连带 import 整个连接层——数据层换成 SQLAlchemy 之后，连接那条路已经
不存在了，时间这条必须活下来，于是搬到本模块。

## 三种精度，各有各的用途

- `utc_now_text()`：**秒级、不带 `Z`**。业务表（用户、方案、任务、配置）用它，
  历史数据全是这个格式，改动等于让新旧值不可比。
- `utc_ms_text()` / `utc_now_ms_text()`：**毫秒级、带 `Z`**。日志用（`app_logs.ts`），
  与日志格式化器共用同一个函数——一边带 `Z` 一边不带会让裁剪静默失效（`'4' < 'Z'`）。
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """当前 UTC 时间（naive，不带时区）——库里一律按 UTC 存，读取时也按 UTC 解释。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_text() -> str:
    """当前 UTC 时间的 ISO 8601 字符串，用于写入库里的日期字段。

    这个格式的好处是**字典序与时间序一致**，可以直接在 SQL 里比较大小
    （例如 `WHERE expires_at > ?`），不必把时间取回 Python 再判断。
    """
    return utc_now().isoformat()


def utc_ms_text(moment: datetime) -> str:
    """把时间转成**毫秒精度、带 `Z` 后缀**的 UTC ISO 8601 字符串。

    这是 `app_logs.ts` 与日志格式化器**共用**的格式。统一它不为好看：
    日志裁剪要用字符串比较时间（`WHERE ts < ?`），一旦一边带 `Z`、另一边不带，
    字典序就会悄悄错位（`'4' < 'Z'`），表现为「该删的没删」且**不报错**。

    带时区的入参会先折算成 UTC 再去掉时区标记：`isoformat()` 对有时区的
    datetime 输出的是 `+00:00` 而不是 `Z`，直接拼一个 `Z` 上去会得到
    `...+00:00Z` 这种两头不靠的格式——正是本函数要防的那种漂移。
    """
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.isoformat(timespec="milliseconds") + "Z"


def utc_now_ms_text() -> str:
    """当前 UTC 时间的毫秒精度字符串（`utc_ms_text(utc_now())` 的简写）。"""
    return utc_ms_text(utc_now())
