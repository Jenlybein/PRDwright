"""结构快照的归一规则（v9 / #78）：只剥「同一结构的不同渲染」，不改语义。

SQLite 专用实现（`sqlite_master` / `PRAGMA table_info` / `index_list` /
`foreign_key_list`）已随 SQLite 后端删除；这份快照现在只有一条读法——SQLAlchemy
inspector 反射（Postgres 上就是 `pg_catalog`），期望形状由 `metadata_snapshot` 按目标
方言编译模型元数据。

归一规则值得单独钉住，因为它们是「不产生幻影迁移」的全部依据：Postgres 反射与模型
编译出来的默认值/类型名会差一层壳（`::text` 后缀、`nextval(...)`、外层引号、括号、
大小写）。少归一任何一条，`prepare_schema` 的 stamp 路径就会把「同一个库」判成漂移。
"""

from __future__ import annotations

import pytest

from backend.core.schema_snapshot import _norm_default, _norm_where, _norm_type


def test_norm_type_uppercases_and_strips():
    """类型名只做大小写与空白归一，不做跨方言映射（那是 SQLite 时代的需求）。"""
    assert _norm_type("varchar(32)") == "VARCHAR(32)"
    assert _norm_type("  text ") == "TEXT"
    assert _norm_type(None) == ""


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        # PG 反射会把 DEFAULT '' 渲染成 ''::text；剥掉类型后缀才是那个值
        ("''::text", ""),
        # 整数默认值同理
        ("'-1'::integer", "-1"),
        # 手写迁移写 ($)，反射写 $，两种渲染归一
        ("('')", ""),
        # 外层引号统一剥掉
        ('"{}"', "{}"),
        # 自增序列的默认值语义上等于「没有显式默认值」
        ("nextval('public.users_id_seq'::regclass)", None),
        ("true", "true"),
    ],
)
def test_norm_default_strips_render_shell_only(declared, expected):
    assert _norm_default(declared) == expected


def test_norm_default_treats_empty_as_none():
    """空串默认值 = 没有默认值：两者在结构比较里必须等价。"""
    assert _norm_default("") is None
    assert _norm_default("   ") is None
    assert _norm_default(None) is None


def test_norm_where_drops_outer_parens_and_whitespace():
    """部分索引的 WHERE 在反射与模型两侧的括号/空白形态不同，必须归一。"""
    assert _norm_where("(job_id IS NOT NULL)") == "job_id IS NOT NULL"
    assert _norm_where("  job_id   IS   NOT NULL ") == "job_id IS NOT NULL"
    assert _norm_where(None) == ""


def test_inspector_snapshot_reads_semantic_shape(isolated_db):  # noqa: ARG001
    """快照的形状是「列 / 索引 / 外键」三元组，且表集合与模型一致。

    这是一条自洽性检查：若反射路径与期望形状用到不同的键，`diff_snapshots` 会把整库
    报成漂移（而那正是它要防的假阳性）。
    """
    from backend.core.engine import get_engine
    from backend.core.schema_snapshot import diff_snapshots, inspector_snapshot, metadata_snapshot

    engine = get_engine()
    actual = inspector_snapshot(engine)
    expected = metadata_snapshot(__import__("sqlmodel").SQLModel.metadata, engine.dialect)
    assert set(actual) == set(expected)
    problems = diff_snapshots(expected, actual)
    assert problems == [], "迁移出来的库结构与模型不一致：\n  " + "\n  ".join(problems)
