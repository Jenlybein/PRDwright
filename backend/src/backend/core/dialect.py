"""方言适配（v9）：全项目**唯一**知道「数据库品牌」的语句构造层。

## 这个模块存在的理由

本项目要写的 SQL 里，只有一件事是 SQLAlchemy 不提供通用抽象的：**upsert**。
官方文档说得很直白——`ON CONFLICT / ON DUPLICATE KEY` 各方言各写各的，
`sqlalchemy.insert()` 上没有跨方言的 `on_conflict_*` 方法。

收敛之前，这件事散在三个仓库里，每个都写 `from sqlalchemy.dialects.sqlite import insert`。
新增一个仓库时漏掉这一行**不会报错**，只会在第一次执行时抛
`AttributeError: 'Insert' object has no attribute 'on_conflict_do_nothing'`。
把差异收进这里之后，调用方只表达语义（「冲突就跳过」还是「冲突就覆盖」），
不再关心是哪家的语法。

## 两条纪律

1. **只做语句构造，不碰事务边界**。这里不 commit / rollback / close，边界仍然只由
   `session_scope()` 决定。
2. **不承载业务判断**。「该不该覆盖」是调用方的决定，本模块只负责翻译。

## 单一后端（#78）

v9 起只支持 Postgres：SQLite / MySQL 家族的分支已删除。保留「未知方言 →
`NotImplementedError`」这一层不是因为「快要用到别的库」，而是为了让
**没适配的后端发不出语句**——静默回落到某个「看起来差不多」的实现，会生成一条在目标库
上语法错误的 SQL，报错指向数据库而不是这里。就绪探针的「真写一次」同理。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Insert, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Result
from sqlmodel import Session, SQLModel


def dialect_name(session: Session) -> str:
    """当前 `Session` 绑定的方言名（本项目只会是 `postgresql`）。

    取 `get_bind()` 而不是读配置：同一个进程里可能同时存在多个库的 Session
    （迁移、对拍、一次性诊断），读配置会拿到「注册时的那个」而不是「这条 Session 真正
    连的那个」。
    """
    return session.get_bind().dialect.name


def _insert_for(model: type[SQLModel], name: str) -> Any:
    """按方言取对应的 `Insert` 构造器。

    未知方言直接抛错，而不是回落到某个「看起来差不多」的实现：
    回落的后果是生成一条在目标库上语法错误的 SQL，错误信息会指向数据库而不是这里。
    """
    if name == "postgresql":
        return postgresql.insert(model)
    raise NotImplementedError(
        f"upsert 尚未适配方言 {name!r}；新增方言请在本模块加一条分支，"
        "不要在调用方写 if（那会把方言重新散开）。"
    )


def _apply_upsert(
    stmt: Insert,
    *,
    name: str,
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None,
) -> Insert:
    """把「冲突怎么办」贴到一个已经构造好的 `Insert` 上。

    抽出来是因为 `upsert_values` 与 `upsert_select` 只在「行从哪来」上不同，
    冲突处理完全共享——两处各写一遍，迟早会有一处漏掉某个分支。
    """
    if name != "postgresql":
        raise NotImplementedError(
            f"upsert 尚未适配方言 {name!r}；新增方言请在本模块加一条分支，"
            "不要在调用方写 if（那会把方言重新散开）。"
        )

    if update_cols is None:
        # `index_elements` 必须给：不给就是「任何唯一冲突都跳过」，语义比调用方
        # 以为的宽，会把别的唯一约束错误一起吞掉。
        return stmt.on_conflict_do_nothing(index_elements=list(conflict_cols))
    return stmt.on_conflict_do_update(
        index_elements=list(conflict_cols),
        set_={col: stmt.excluded[col] for col in update_cols},
    )


def upsert_values(
    session: Session,
    model: type[SQLModel],
    values: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
) -> Result:
    """插入常量行；冲突时按 `update_cols` 覆盖，或（`None`）什么都不做。

    Args:
        session: 绑定了目标库的 Session（不在这里提交）。
        model: 表模型。
        values: 一行（dict）或多行（dict 列表）。
        conflict_cols: 冲突判定用的列——**必须是该表唯一的那个约束**（契约 C-D1）。
        update_cols: 冲突时要覆盖的列；`None` 表示「冲突就跳过」。

    Returns:
        执行结果（调用方通常只看 `rowcount`）。

    Example:
        >>> upsert_values(  # doctest: +SKIP
        ...     session, SystemConfig, {"key": "a", "value": "1", ...},
        ...     conflict_cols=["key"], update_cols=["value", ...],
        ... )
    """
    name = dialect_name(session)
    stmt = _insert_for(model, name).values(values)
    return session.execute(
        _apply_upsert(
            stmt,
            name=name,
            conflict_cols=conflict_cols,
            update_cols=update_cols,
        )
    )


def upsert_select(
    session: Session,
    model: type[SQLModel],
    columns: Sequence[str],
    select_stmt: Any,
    *,
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
) -> Result:
    """`INSERT ... SELECT` 形态的 upsert。

    与 `upsert_values` 分开是一个设计选择，不是重载：`document_repo._ensure_document`
    把**归属校验写进了 SELECT 的 WHERE**（方案不属于该用户时一行都插不进去，不需要
    「先查一次再写」这种两次往返）。那个形状必须保留，所以这里显式接受一个 SELECT。

    契约 **C-D1**：`conflict_cols` 必须是该表唯一的那个约束；PG 的 `ON CONFLICT`
    只对给出的目标列生效，但调用方仍应按这条契约束手。

    Args:
        columns: 目标列名，顺序与 SELECT 的输出列一一对应。
        select_stmt: 提供数据的 SELECT（可带任意 WHERE，包括归属过滤）。
        conflict_cols: 同 `upsert_values`。
        update_cols: 同 `upsert_values`。
    """
    name = dialect_name(session)
    stmt = _insert_for(model, name).from_select(list(columns), select_stmt)
    return session.execute(
        _apply_upsert(
            stmt,
            name=name,
            conflict_cols=conflict_cols,
            update_cols=update_cols,
        )
    )


# ---------------------------------------------------------------------------
# 就绪探针的「真写一次」
# ----------------------------------------------------------------------------


def write_probe(connection: Any) -> None:
    """在当前事务里做一次**内容不变的真写**，用于就绪探针（调用方负责回滚）。

    ## 为什么不能通用一句话

    「库可写」这条判据必须靠**真写**才测得出来：只读实例、只读挂载、磁盘满后的
    半只读状态，读得出来、写不进去。

    单一后端（#78）之后只剩一套写法：**Postgres 的 DDL 可回滚**，所以建一张临时表
    就是「真写一次」——只读实例 / 只读事务会当场报
    `cannot execute CREATE TABLE in a read-only transaction`，回滚后临时表
    随事务一起消失，不留痕迹。（SQLite 的 `BEGIN IMMEDIATE` + `PRAGMA` 与 MySQL 的
    「内容不变 UPDATE」两条分支已随对应后端一起删除。）

    ⚠️ **不在这里 commit / rollback**：事务边界仍归调用方（`api/health.py` 先
    `conn.begin()`，`finally` 里 `rollback()`）。本模块只回答「这个方言该发哪句」。

    Args:
        connection: 已经开好事务的 SQLAlchemy Connection（不会被关闭）。

    Raises:
        NotImplementedError: 未适配的方言。宁可报错，也不要发一句在目标库上
            语法不通的探针——那会把「没适配」伪装成「库坏了」。
    """
    name = connection.dialect.name
    if name == "postgresql":
        connection.exec_driver_sql("CREATE TEMP TABLE prdwright_write_probe (id integer)")
        return
    raise NotImplementedError(
        f"就绪探针尚未适配方言 {name!r}；新增方言请在本模块加一条分支，"
        "不要在调用方写 if（那会把方言重新散开）。"
    )


__all__ = ["dialect_name", "upsert_select", "upsert_values", "write_probe"]
