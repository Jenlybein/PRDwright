"""列类型策略（v8）：全项目**唯一**决定「一列用什么类型」的地方。

## 为什么需要它

SQLite 不校验长度，所以「这一列是 `TEXT` 还是 `VARCHAR(64)`」在 SQLite 上**看不出差别**——
两种写法都存得下、都读得回。差别只在一个地方出现：**MySQL 的索引**。

`TEXT` 列在 MySQL 上不能整列建索引、不能做主键（必须给前缀长度，而前缀唯一 ≠ 全值唯一）。
于是「某列能不能建索引」这件事，在 SQLite 上是自由的，在 MySQL 上是硬约束。
把这条约束集中在三个构造函数里，是唯一能让它在两个后端上都成立的办法。

## 三条规则

1. **要建索引（含主键、唯一约束）的列必须用 `short_text(n)`**，且 `n ≤ 768`；
2. **大段文本用 `long_text()`**，它不许进索引、不许带 `server_default`；
3. 其余普通字符串列仍可用 `Text`（不索引，MySQL 下就是 `TEXT`）。

第 3 条是刻意的：把所有列都改成 `VARCHAR(n)` 会让改动面大到无法验证，而这些列
本来就不索引。**只改必须改的**。

## 为什么 n ≤ 768

InnoDB 单列索引上限 3072 字节，`utf8mb4` 每字符最多 4 字节 → 768 字符。
超过这个数，MySQL 建索引会报 `Specified key was too long`。

## 为什么大文本不能带默认值

MySQL 的 `BLOB/TEXT` 列不接受字面量默认值（8.0.13 之前完全不行，之后也只能用
表达式 `DEFAULT ('')`）。我们选择**不给默认值**，靠应用层显式赋值——ORM 插入本来
就总是带值，而"裸 SQL 省略这一列"应该失败（它是显式错误，不是静默的空串）。

## 自检在 import 时跑

`assert_metadata_is_portable()` 在 `models.py` 末尾调用。这让「索引列没给长度」
这类错误**发生在 import 那一刻**，而不是等到某天在 MySQL 上跑迁移时才发现。
"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.sql.type_api import TypeEngine

# InnoDB 单列索引 3072 字节 ÷ utf8mb4 的 4 字节
MAX_INDEX_CHARS = 768

# 时间戳串的固定宽度：`utc_now_text()` 产出 26 字符，`utc_ms_text()` 24 字符（带 Z）。
# 取 32 留出「将来加时区后缀」的余量，同时保持它是可索引的短类型。
TIMESTAMP_CHARS = 32


def short_text(max_length: int) -> TypeEngine:
    """有长度上限的短文本，**可用于索引 / 主键 / 唯一约束**。

    Args:
        max_length: 字符数上限。SQLite 不校验它；MySQL 的 `VARCHAR(n)` 会校验。

    Raises:
        ValueError: 长度非正或超过 `MAX_INDEX_CHARS`（MySQL 建不了索引）。
    """
    if not 1 <= max_length <= MAX_INDEX_CHARS:
        raise ValueError(
            f"short_text({max_length}) 超出范围 1..{MAX_INDEX_CHARS}；"
            "MySQL 的单列索引上限是 3072 字节（utf8mb4 下 768 字符）。"
        )
    return String(max_length)


def long_text() -> TypeEngine:
    """大段文本：SQLite `TEXT` / MySQL `MEDIUMTEXT`（16MB）。

    容量不是业务约束——「最多 60KB」由 `core/limits.py` 在写入前拦。
    """
    return Text().with_variant(mysql.MEDIUMTEXT(), "mysql")


def timestamp_text() -> TypeEngine:
    """时间戳串（ISO 8601 文本）。

    为什么不改成真的 `DATETIME`：本项目靠「ISO 字符串的字典序 = 时间序」做比较，
    改成 `DATETIME` 会同时改变落盘格式、精度与时区解释，
    代价远大于收益。这里只把宽度定下来，让它能建索引。
    """
    return short_text(TIMESTAMP_CHARS)


# ---------------------------------------------------------------------------
# 可移植性自检
# ---------------------------------------------------------------------------


def _impl(type_: TypeEngine) -> TypeEngine:
    """剥掉 `Variant` 外壳，拿到实际类型。

    `long_text()` 返回的是 `Variant`，`isinstance(x, Text)` 对它是 False，
    而 `getattr(x, "length")` 又被代理到 impl——不统一剥壳会让判断逻辑分裂。
    """
    return getattr(type_, "impl", type_)


def _indexed_columns(table) -> set[str]:  # noqa: ANN001 - SQLAlchemy Table
    """该表所有「被索引」的列名：索引 + 主键 + 唯一约束。

    唯一约束在 SQLAlchemy 里既可能是 `UniqueConstraint`，也可能是列级 `unique=True`
    （后者不会出现在 `table.constraints` 的 UniqueConstraint 里，要单独看列）。
    """
    names: set[str] = {col.name for col in table.primary_key.columns}
    for index in table.indexes:
        names.update(col.name for col in index.columns)
    for constraint in table.constraints:
        if constraint.__class__.__name__ == "UniqueConstraint":
            names.update(col.name for col in constraint.columns)
    for col in table.columns:
        if col.unique:
            names.add(col.name)
    return names


def assert_metadata_is_portable(metadata) -> None:  # noqa: ANN001 - MetaData
    """校验整份 metadata 在 MySQL 上建得出来。

    两条检查，都是「在 SQLite 上完全正常、到 MySQL 才炸」的类型：

    1. 索引列必须是 `short_text(n)`（`n ≤ 768`）——否则建索引时会
       `Specified key was too long`，或主键直接建不出来；
    2. 无长度上限的文本列不能带 `server_default`——否则报
       `BLOB, TEXT, GEOMETRY or JSON column can't have a default value`。

    Raises:
        RuntimeError: 存在任一问题。消息里列出全部违规列，便于一次改完。
    """
    problems: list[str] = []

    for table in metadata.tables.values():
        indexed = _indexed_columns(table)
        for col in table.columns:
            impl = _impl(col.type)
            if not isinstance(impl, String):
                continue
            length = getattr(impl, "length", None)

            if col.name in indexed:
                if length is None:
                    problems.append(
                        f"{table.name}.{col.name}：列被索引但没有长度上限，"
                        "MySQL 建不了索引；改用 short_text(n)"
                    )
                elif length > MAX_INDEX_CHARS:
                    problems.append(
                        f"{table.name}.{col.name}：长度 {length} 超过 {MAX_INDEX_CHARS}，"
                        "MySQL 建不了索引"
                    )
            elif length is None and col.server_default is not None:
                problems.append(
                    f"{table.name}.{col.name}：无长度文本带了 server_default，"
                    "MySQL 建表会报 can't have a default value；去掉默认值"
                )

    if problems:
        detail = "\n  ".join(problems)
        raise RuntimeError(
            "列类型在 MySQL 上不可移植（在 SQLite 上看不出来）：\n"
            f"  {detail}\n"
            "规则见 core/types.py 的模块 docstring。"
        )


__all__ = [
    "MAX_INDEX_CHARS",
    "TIMESTAMP_CHARS",
    "assert_metadata_is_portable",
    "long_text",
    "short_text",
    "timestamp_text",
]
