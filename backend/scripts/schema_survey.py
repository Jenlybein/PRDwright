"""库结构勘察（只读）：为「定长度」提实测依据。

实测数据与列长度取值来源见 `docs/architecture/ARCHITECTURE.md 附录 B`；
方言可移植性台账与 C-D1~C-D6 见同文件附录 A。

## 为什么需要它

`models.py` 里所有字符串列都是 `Column(Text)`。SQLite 不校验长度，
所以"现在能存"不等于"换库后还能存"。可选目标库的硬约束是：

- MySQL：`TEXT` 不能直接建索引/主键（要前缀长度）；单列上限 64KB；
- 所有库：唯一约束一旦真的生效，历史重复值会让迁移失败。

要定 `VARCHAR(n)` 的 n 和 `MEDIUMTEXT` 够不够，只能量真实数据——猜出来的 n
一旦小于线上最大值，迁移当天就会报 `Data too long`。

## 三条纪律

1. **只读**：以 `file:...?mode=ro` 打开，写操作在物理上不可能发生；
2. **不猜**：每一项都打印实测值，不做「应该够」的假设；
3. **可复核**：每个结论都能用一句 SQL 复现，写进方案文档时可追溯。

## 用法

    python backend/scripts/schema_survey.py
    python backend/scripts/schema_survey.py --db /var/lib/prdwright/prdwright.db
    python backend/scripts/schema_survey.py --out docs/architecture/ARCHITECTURE.md 附录 B

不带 `--db` 时读 `backend/prdwright.db`（与 `core/paths.default_db_file()` 同口径）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

# 长度档位：报告里按这些阈值统计"有多少行超过"，供定 n 时取上界。
THRESHOLDS = (64, 128, 255, 512, 1024, 4096, 16384, 65536, 1048576)

# 字节档位：MySQL 的 `TEXT` 上限 64KB、`MEDIUMTEXT` 16MB，都是**字节**。
# 中文一个字占 3 字节，所以这两个数才是"够不够"的真正分界。
BYTE_THRESHOLDS = (65536, 16777216)

# 布尔列白名单：与 `core/models.py::BOOLEAN_COLUMNS` 同源。
#
# 这里抄一份而不是 import：勘察脚本要能在一台只放了源码树、没装项目依赖的
# 服务器上直接跑（`python backend/scripts/schema_survey.py --db ...`）。
# 两份清单**靠人工对齐**：`tests/test_schema_parity.py` 只对拍「模型 ↔ 库」，
# 不读本脚本，所以删改布尔列时必须同时改这里（#9 删 `builtin` 时这份副本就落后过一拍）。
BOOLEAN_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("users", "is_active"),
        ("users", "is_admin"),
        ("model_providers", "enabled"),
        ("model_providers", "thinking_enabled"),
        ("chat_turns", "interrupted"),
    }
)


def open_readonly(db: Path) -> sqlite3.Connection:
    """以只读模式打开库。

    `mode=ro` 是 URI 参数，必须 `uri=True` 才生效。少了它，
    `sqlite3.connect` 会在文件不存在时**创建一个空库**——勘察脚本静默造出一个
    空文件，然后报告"0 张表"，比报错难查得多。
    """
    uri = f"file:{db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn: sqlite3.Connection) -> list[str]:
    """业务表（排除 SQLite 与 Alembic 自己的账本）。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [
        r["name"]
        for r in rows
        if not r["name"].startswith("sqlite_") and r["name"] != "alembic_version"
    ]


def columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    """列定义（`PRAGMA table_info`：cid, name, type, notnull, dflt_value, pk）。"""
    return conn.execute(f'PRAGMA table_info("{table}")').fetchall()


def index_columns(conn: sqlite3.Connection, table: str) -> dict[str, list[str]]:
    """列名 → 所在索引名清单。用来判断"这一列要不要能建索引"。"""
    out: dict[str, list[str]] = {}
    for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        name = row["name"]
        for info in conn.execute(f'PRAGMA index_info("{name}")').fetchall():
            col = info["name"]
            if col:
                out.setdefault(col, []).append(name)
    return out


def unique_indexes(conn: sqlite3.Connection, table: str) -> list[tuple[str, list[str]]]:
    """该表的唯一索引（含 `origin='u'` 的 UNIQUE 约束自动索引）。

    返回 `[(索引名或 <unique-constraint>, [列...])]`。换库时这些约束会**真的生效**，
    所以要先确认历史数据里没有重复。
    """
    out: list[tuple[str, list[str]]] = []
    for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        if not row["unique"]:
            continue
        cols = [
            info["name"]
            for info in conn.execute(f'PRAGMA index_info("{row["name"]}")').fetchall()
            if info["name"]
        ]
        if cols:
            label = "<unique-constraint>" if row["origin"] == "u" else row["name"]
            out.append((label, cols))
    return out


def is_text_type(declared: str | None) -> bool:
    """按 SQLite 的亲和性判断"是不是可以存任意长文本"的列。

    `TEXT` / `VARCHAR(n)` / `CLOB` 的亲和性都是 TEXT。声明成 `VARCHAR(n)` 的列
    在 SQLite 里**同样不校验长度**，所以现有库即使写了 `VARCHAR(64)` 也可能存着
    300 字符——这个函数把它们一视同仁地量出来。
    """
    kind = (declared or "").upper()
    return any(token in kind for token in ("TEXT", "CHAR", "CLOB"))


def length_stats(conn: sqlite3.Connection, table: str, col: str) -> dict[str, Any]:
    """单列的长度分布（字符数 + **字节数**）。

    两个长度都要量，因为它们回答的是不同的问题：

    - **字符数**决定 `VARCHAR(n)` 的 n（SQL 标准的 `CHAR_LENGTH` 语义）；
    - **字节数**决定 `TEXT` 够不够。MySQL 的 `TEXT` 上限 64KB 是**字节**，
      而中文一个字 3 字节 —— 一万字的 PRD 就是 30KB，离 64KB 只剩一倍余量。
      只看字符数会得出「才 11566，很安全」的错误结论。

    `length(CAST(col AS BLOB))` 取的是 UTF-8 编码后的字节数：SQLite 的
    `length()` 对二进制返回字节、对文本返回字符，这一转换把语义显式钉死。

    分位数用 `ORDER BY ... LIMIT 1 OFFSET n`：SQLite 没有 percentile 函数。
    ⚠️ OFFSET 必须在 **非 NULL 行数** 上算——用总行数算会落到 NULL 区间外，
    返回空值，然后被默默当成 0（第一版就踩了这个坑：
    `app_logs.request_id` 有 769 行 NULL，p99 报成了 0）。
    """
    where = f'"{col}"'
    byte_len = f"length(CAST({where} AS BLOB))"
    row = conn.execute(
        f'SELECT count(*) AS total, '
        f'max(length({where})) AS max_len, '
        f'max({byte_len}) AS max_bytes, '
        f'sum({where} IS NULL) AS null_count, '
        f'sum(length({where}) = 0) AS empty_count '
        f'FROM "{table}"'
    ).fetchone()

    total = int(row["total"] or 0)
    null_count = int(row["null_count"] or 0)
    stats: dict[str, Any] = {
        "total": total,
        "max": int(row["max_len"] or 0),
        "max_bytes": int(row["max_bytes"] or 0),
        "null": null_count,
        "empty": int(row["empty_count"] or 0),
        "p50": 0,
        "p99": 0,
        "over": {},
        "over_bytes": {},
    }
    if total == 0 or total - null_count <= 0:
        return stats

    present = total - null_count
    for label, pct in (("p50", 0.50), ("p99", 0.99)):
        offset = max(0, min(present - 1, int(present * pct)))
        picked = conn.execute(
            f'SELECT length({where}) AS n FROM "{table}" '
            f'WHERE {where} IS NOT NULL ORDER BY length({where}) LIMIT 1 OFFSET ?',
            (offset,),
        ).fetchone()
        stats[label] = int(picked["n"] or 0) if picked else 0

    # 一条 SQL 数完所有阈值：比逐阈值扫 9 遍快，也少 9 次误读的机会
    sums = ", ".join(f'sum(length({where}) > {limit})' for limit in THRESHOLDS)
    sums += ", " + ", ".join(
        f"sum({byte_len} > {limit})" for limit in BYTE_THRESHOLDS
    )
    over = conn.execute(f'SELECT {sums} FROM "{table}"').fetchone()
    for i, limit in enumerate(THRESHOLDS):
        count = int(over[i] or 0)
        if count:
            stats["over"][limit] = count
    base = len(THRESHOLDS)
    for i, limit in enumerate(BYTE_THRESHOLDS):
        count = int(over[base + i] or 0)
        if count:
            stats["over_bytes"][limit] = count
    return stats


def duplicate_check(
    conn: sqlite3.Connection, table: str, cols: list[str], *, fold: bool = False
) -> tuple[int, list[tuple[Any, ...]]]:
    """按列组合找重复。`fold=True` 时先做 `lower()`（模拟不区分大小写的 collation）。

    换库后 MySQL 默认 collation 不区分大小写，所以对 `users.username` 这类列，
    必须同时量「精确重复」和「忽略大小写后的重复」——后者才是换库后的真实行为。
    """
    selector = ", ".join(f'lower("{c}")' if fold else f'"{c}"' for c in cols)
    group = ", ".join(str(i + 1) for i in range(len(cols)))
    rows = conn.execute(
        f'SELECT {selector} AS k, count(*) AS c FROM "{table}" '
        f'WHERE {" AND ".join(f'"{c}" IS NOT NULL' for c in cols)} '
        f"GROUP BY {group} HAVING c > 1 ORDER BY c DESC LIMIT 5"
    ).fetchall()
    total = conn.execute(
        f'SELECT count(*) AS c FROM (SELECT {selector} FROM "{table}" '
        f'WHERE {" AND ".join(f'"{c}" IS NOT NULL' for c in cols)} '
        f"GROUP BY {group} HAVING count(*) > 1)"
    ).fetchone()["c"]
    return int(total or 0), [tuple(r) for r in rows]


def boolean_values(conn: sqlite3.Connection, table: str) -> list[tuple[str, list[Any]]]:
    """白名单里的布尔列 → 实际取值。

    **不用"取值是否 ⊆ {0,1}"来猜**：`model_providers.sort_order` 在全表恰好只有 0
    的时候完全满足这个条件，会被误报成布尔列（第一版就把 4 个整型列认错了，
    包括 `context_window` 和 `max_output` 这种语义上绝不可能是布尔的列）。

    布尔与否是**模型的事实**，不是数据分布的事实。所以这里读白名单，
    再验证库里的取值是否都合规——这才是这条检查真正要回答的问题：
    「换库后 `BOOLEAN` ↔ `TINYINT(1)` 的映射会不会丢掉非法值」。
    """
    out: list[tuple[str, list[Any]]] = []
    for col in columns(conn, table):
        if (table, col["name"]) not in BOOLEAN_COLUMNS:
            continue
        values = [
            r[0]
            for r in conn.execute(
                f'SELECT DISTINCT "{col["name"]}" FROM "{table}" LIMIT 5'
            ).fetchall()
        ]
        out.append((col["name"], values))
    return out


def timestamp_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """时间列：命名以 `_at` 结尾或叫 `ts`（与代码里的 `utc_now_text()` 口径对应）。"""
    return [
        col["name"]
        for col in columns(conn, table)
        if col["name"].endswith("_at") or col["name"] == "ts"
    ]


def timestamp_shape(conn: sqlite3.Connection, table: str, col: str) -> dict[str, int]:
    """时间串的形态统计。

    `utc_now_text()` 产出 `YYYY-MM-DDTHH:MM:SS.ffffffZ`。换库后如果这些列要改成
    真的 `DATETIME`，就必须先确认**没有**混进本地时间或其他格式——混合格式在
    文本比较下仍是"能排序"，但一旦转成 DATETIME 就会解析失败或错 8 小时。

    ⚠️ 通配符用 `?` 不是 `_`：`_` 是 `LIKE` 的单字符通配，`GLOB` 里它只是字面的
    下划线。第一版写成 `____-__-__T...` 时，全部 1905 行都被判成"异常"——
    一个只看输出的读者会以为库里的时间格式全乱了。

    `dirty` 一列查的是 `...+00:00Z` 这种**两头不靠**的格式：`utc_ms_text` 的
    docstring 专门写了要防它（`isoformat()` 对带时区的值输出 `+00:00` 而不是
    `Z`，再拼一个 `Z` 就成了这个怪东西）。函数已经修了，但**旧数据还在库里**——
    换库时它会让 `DATETIME` 解析失败。这条检查是本次勘察唯一捞出脏数据的地方。
    """
    row = conn.execute(
        f'SELECT sum("{col}" IS NULL) AS n_null, '
        f"""sum("{col}" GLOB '????-??-??T??:??:??*') AS n_iso, """
        f"""sum("{col}" GLOB '????-??-??T??:??:??*Z') AS n_utc, """
        f"""sum("{col}" LIKE '%+__:__Z' OR "{col}" LIKE '%-__:__Z') AS n_dirty, """
        f'count(*) AS n_total FROM "{table}"'
    ).fetchone()
    return {
        "total": int(row["n_total"] or 0),
        "null": int(row["n_null"] or 0),
        "iso_like": int(row["n_iso"] or 0),
        "utc_z": int(row["n_utc"] or 0),
        "dirty": int(row["n_dirty"] or 0),
    }


def suggest_varchar(stats: dict[str, Any]) -> int:
    """给一个"够用但不浪费"的档位：实测最大值向上取到常见档位。

    上取而不是恰好等于 max：max 只是这一份数据的最大值，生产库可能更长。
    取到下一档留一倍余量，同时避免 `VARCHAR(37)` 这种一眼看不出意图的写法。

    ⚠️ 这只是**建议**，不是结论：真正定 n 要在生产库上跑同一份脚本，
    并且给 `users.username` 这类无上限的用户输入补应用层校验。
    """
    need = int(stats.get("max") or 0)
    for limit in (32, 64, 128, 255, 512, 1024, 4096, 16384):
        if need <= limit:
            return limit
    return 0  # 超过 16384：不适合 VARCHAR，应保留 TEXT/MEDIUMTEXT


def survey(db: Path) -> str:
    """跑完整勘察，返回 markdown 报告。"""
    lines: list[str] = []
    add = lines.append

    with closing(open_readonly(db)) as conn:
        tables = table_names(conn)
        total_rows = 0
        rows_by_table: dict[str, int] = {}
        for table in tables:
            count = int(
                conn.execute(f'SELECT count(*) AS c FROM "{table}"').fetchone()["c"]
            )
            rows_by_table[table] = count
            total_rows += count

        add(f"# 库结构勘察：{db.name}")
        add("")
        add(
            f"- 表 {len(tables)} 张，共 {total_rows} 行，文件 {db.stat().st_size / 1024:.0f} KB"
        )
        add(
            f"- 只读打开（`mode=ro`），不存在任何写入路径"
        )
        add("")

        # ---- 1. 行数分布 ----
        add("## 1 表的规模")
        add("")
        add("| 表 | 行数 |")
        add("| --- | ---: |")
        for table in tables:
            add(f"| `{table}` | {rows_by_table[table]} |")
        add("")

        # ---- 2. 文本列长度 ----
        add("## 2 文本列的长度分布")
        add("")
        add(
            "`超字符阈值` 只列出**有命中**的档位。`索引` 一列是「该列是否需要能建索引」——"
            "MySQL 下 `TEXT` 列建索引必须给前缀长度，主键更是完全不行。"
        )
        add("")
        add(
            "`max字节` 与 `超64KB` 才是 `TEXT` 够不够的依据：MySQL 的 `TEXT` 上限 64KB 按**字节**算，"
            "中文一字 3 字节——11500 字的中文 PRD 已经是 34KB，不是「才 11K」。"
        )
        add("")
        add(
            "| 表.列 | 行 | max字 | max字节 | p99 | NULL | 超字符阈值 | 超64KB | 索引 | 建议 |"
        )
        add("| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |")

        suggestions: list[tuple[str, str, int]] = []
        for table in tables:
            idx_map = index_columns(conn, table)
            for col in columns(conn, table):
                if not is_text_type(col["type"]):
                    continue
                stats = length_stats(conn, table, col["name"])
                if stats["total"] == 0:
                    continue
                over = "、".join(f">{k}: {v}" for k, v in stats["over"].items()) or "—"
                over_bytes = stats["over_bytes"].get(65536, 0) or "—"
                index_names = idx_map.get(col["name"], [])
                index_label = "、".join(index_names) if index_names else "—"
                want = suggest_varchar(stats) if index_names else 0
                if index_names:
                    label = f"`VARCHAR({want})`" if want else "保留 TEXT"
                    suggestions.append((table, col["name"], want))
                else:
                    label = "保留 TEXT"
                add(
                    f"| `{table}.{col['name']}` | {stats['total']} | {stats['max']} | "
                    f"{stats['max_bytes']} | {stats['p99']} | {stats['null']} | "
                    f"{over} | {over_bytes} | {index_label} | {label} |"
                )
        add("")

        # ---- 3. 唯一约束 ----
        add("## 3 唯一约束的重复检查")
        add("")
        add(
            "SQLite 这边约束**已经生效**（`PRAGMA foreign_keys=ON`），所以正常情况下不该有重复。"
            "真正要检查的是 **`fold` 一列**：换库后 MySQL 默认 collation 不区分大小写，"
            "`Admin` 与 `admin` 会变成同一个键。"
        )
        add("")
        add("| 表 | 约束 | 列 | 精确重复 | 忽略大小写重复 |")
        add("| --- | --- | --- | ---: | ---: |")
        for table in tables:
            for label, cols in unique_indexes(conn, table):
                exact, _ = duplicate_check(conn, table, cols)
                folded, _ = duplicate_check(conn, table, cols, fold=True)
                add(
                    f"| `{table}` | {label} | {', '.join(cols)} | {exact} | "
                    f"{folded if folded != exact else '同左'} |"
                )
        add("")

        # ---- 4. 布尔列 ----
        add("## 4 布尔列的取值合规性")
        add("")
        add(
            "按 `models.BOOLEAN_COLUMNS` 白名单检查，**不按数据分布猜**——"
            "`model_providers.sort_order` 在全表恰好只有 0 的时候也满足「只落在 0/1」。"
            "这张表回答的是：换库后 `BOOLEAN` ↔ `TINYINT(1)` 的映射会不会丢掉非法值。"
        )
        add("")
        add("| 表.列 | 实测取值 | 合规 |")
        add("| --- | --- | --- |")
        bool_problems: list[str] = []
        for table in tables:
            for col, values in boolean_values(conn, table):
                legal = set(values) <= {0, 1, None}
                if not legal:
                    bool_problems.append(f"`{table}.{col}`")
                shown = "、".join("NULL" if v is None else str(v) for v in values) or "（无行）"
                add(f"| `{table}.{col}` | {shown} | {'是' if legal else '**否**'} |")
        add("")
        if bool_problems:
            add(f"**越界值**：{'、'.join(bool_problems)}（迁移前必须清洗）")
        else:
            add("白名单内所有布尔列的取值都在 `{0, 1, NULL}` 内。")
        add("")

        # ---- 5. 时间列形态 ----
        add("## 5 时间列的格式一致性")
        add("")
        add(
            "`UTC(Z)` 是 `utc_now_text()` 的规范形态。`ISO` 但**不带** Z 的行需要盯——"
            "那可能混进了本地时间，换库改成 `DATETIME` 时会错 8 小时。"
        )
        add("")
        add("| 表.列 | 行 | NULL | ISO 形态 | 带 Z | 非 Z | 脏格式 | 格式异常 |")
        add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        shape_problems: list[str] = []
        dirty_problems: list[str] = []
        for table in tables:
            for col in timestamp_columns(conn, table):
                shape = timestamp_shape(conn, table, col)
                if shape["total"] == 0:
                    continue
                # 「异常」只算"连 ISO 形状都不像"的：`utc_z ⊆ iso_like`，
                # 所以拿 iso_like 做基数，否则带 Z 的行会被重复扣两次。
                abnormal = shape["total"] - shape["null"] - shape["iso_like"]
                no_z = shape["iso_like"] - shape["utc_z"]
                if abnormal:
                    shape_problems.append(f"`{table}.{col}`（{abnormal} 行）")
                if shape["dirty"]:
                    dirty_problems.append(f"`{table}.{col}`（{shape['dirty']} 行）")
                add(
                    f"| `{table}.{col}` | {shape['total']} | {shape['null']} | "
                    f"{shape['iso_like']} | {shape['utc_z']} | {no_z or '—'} | "
                    f"{shape['dirty'] or '—'} | {abnormal or '—'} |"
                )
        add("")
        if shape_problems:
            add(f"**格式不合规**：{'、'.join(shape_problems)}——迁移成 `DATETIME` 前必须清洗。")
        else:
            add("所有时间列都形如 `YYYY-MM-DDTHH:MM:SS…`，可安全转 `DATETIME`。")
        if dirty_problems:
            add("")
            add(
                f"**脏格式（`+00:00Z`）**：{'、'.join(dirty_problems)}——`DATETIME` 解析会直接失败，"
                "必须在迁移脚本里剥掉偏移再补 `Z`。"
            )
        add("")

        # ---- 6. 派生结论 ----
        add("## 6 由数据推出的候选长度")
        add("")
        if suggestions:
            add("| 表.列 | 实测 max字 | 实测 max字节 | 建议档位 |")
            add("| --- | ---: | ---: | --- |")
            for table, col, want in suggestions:
                stats = length_stats(conn, table, col)
                add(
                    f"| `{table}.{col}` | {stats['max']} | {stats['max_bytes']} | "
                    f"{f'`VARCHAR({want})`' if want else '保留 TEXT（超 16K）'} |"
                )
        else:
            add("（没有被索引的文本列）")
        add("")
        add("> 这些档位只反映**当前这个库**。生产库必须用同一份脚本再跑一次——")
        add("> `VARCHAR(n)` 的 n 小于线上最大值时，迁移当天会以 `Data too long` 失败。")
        add("")

        # ---- 7. 需要 MEDIUMTEXT 的列 ----
        add("## 7 超过 16KB 的未索引文本列")
        add("")
        add(
            "这些列不建索引，所以不受 `VARCHAR` 约束；但 MySQL 的 `TEXT` 只有 64KB（字节），"
            "超过 16KB 就建议直接声明成 `MEDIUMTEXT`，别赌「还能再长一点」。"
        )
        add("")
        big: list[tuple[str, str, int, int]] = []
        for table in tables:
            idx_map = index_columns(conn, table)
            for col in columns(conn, table):
                if not is_text_type(col["type"]) or col["name"] in idx_map:
                    continue
                stats = length_stats(conn, table, col["name"])
                if stats["max_bytes"] > 16384:
                    big.append((table, col["name"], stats["max"], stats["max_bytes"]))
        if big:
            add("| 表.列 | max字 | max字节 | 建议 |")
            add("| --- | ---: | ---: | --- |")
            for table, col, chars, size in big:
                add(f"| `{table}.{col}` | {chars} | {size} | `MEDIUMTEXT` |")
        else:
            add("（没有超过 16KB 的未索引文本列）")
        add("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读勘察 SQLite 库的列长度、唯一约束与时间格式"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "prdwright.db",
        help="数据库文件路径（默认 backend/prdwright.db）",
    )
    parser.add_argument("--out", type=Path, default=None, help="把报告写到文件")
    args = parser.parse_args(argv)

    if not args.db.exists():
        # 明确报错，不创建空库：扫出一个"0 张表"的报告比直接失败更难查
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2

    report = survey(args.db)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n", encoding="utf-8")
        print(f"报告已写入：{args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
