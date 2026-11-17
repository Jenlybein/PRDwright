"""列类型纪律（v9 / #78）：`core/types.py` 的两条规则与三个构造函数。

v8 时代这里还守过「MySQL 的 `MEDIUMTEXT` / utf8mb4 collation 变体」；#78 收缩到
Postgres 后那些变体已删除，剩下的规则与后端无关：

1. **索引列必须有界**（`short_text(n)`，n≤768）——B-tree 条目上限；
2. **大文本不许带 `server_default`**——否则裸 SQL 省略该列时会静默写入空串。

这两条由 `assert_metadata_is_portable()` 在 import 时自检（`models.py` 末尾调用）。
这里用**临时的 metadata** 复现违规形状，证明自检真的会拦——而不是只相信「现在没触发」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Index, MetaData, String, Table, Text, text

from backend.core import models  # noqa: F401 - 导入即跑一次真实自检
from backend.core.types import (
    MAX_INDEX_CHARS,
    TIMESTAMP_CHARS,
    assert_metadata_is_portable,
    long_text,
    short_text,
    timestamp_text,
)


def test_short_text_bounds_are_enforced():
    """`short_text(n)` 只接受 1..768：超出上界就是「索引列会超 B-tree 条目」的信号。"""
    assert isinstance(short_text(32), String)
    assert short_text(32).length == 32
    assert short_text(MAX_INDEX_CHARS).length == MAX_INDEX_CHARS

    for bad in (0, -1, MAX_INDEX_CHARS + 1):
        with pytest.raises(ValueError):
            short_text(bad)


def test_long_text_is_plain_text():
    """`long_text()` 现在是纯 `Text`（不带任何方言变体）。

    v8 的 `mysql.MEDIUMTEXT` variant 已随 MySQL 后端删除；如果它回来，Postgres 上会
    多一层无用的 variant 外壳，结构对拍的类型编译也会出噪音。
    """
    type_ = long_text()
    assert type(type_).__name__ == "Text"
    assert type_.length is None


def test_timestamp_text_is_indexable_width():
    """时间戳串固定 32 字符宽——它被索引，所以必须是有界类型。"""
    type_ = timestamp_text()
    assert isinstance(type_, String)
    assert type_.length == TIMESTAMP_CHARS


def test_portability_guard_rejects_indexed_text_column():
    """索引列没有长度上限时，import 期自检必须报错（而不是等生产迁移才失败）。"""
    metadata = MetaData()
    Table(
        "t_bad_index",
        metadata,
        Column("id", String(8), primary_key=True),
        Column("loose", Text, nullable=False),
        Index("idx_loose", "loose"),
    )
    with pytest.raises(RuntimeError) as caught:
        assert_metadata_is_portable(metadata)
    assert "t_bad_index.loose" in str(caught.value)


def test_portability_guard_rejects_long_text_server_default():
    """大文本带 `server_default` 时同样要拦：省略该列会静默写成空串。"""
    metadata = MetaData()
    Table(
        "t_bad_default",
        metadata,
        Column("id", String(8), primary_key=True),
        Column("blob", Text, nullable=False, server_default=text("''")),
    )
    with pytest.raises(RuntimeError) as caught:
        assert_metadata_is_portable(metadata)
    assert "t_bad_default.blob" in str(caught.value)
