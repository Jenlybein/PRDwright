"""方言适配（v8）：全项目**唯一**知道「数据库品牌」的语句构造层。

## 这个模块存在的理由

本项目要写的 SQL 里，只有一件事是 SQLAlchemy 不提供通用抽象的：**upsert**。
官方文档说得很直白——`ON CONFLICT` / `ON DUPLICATE KEY` 各方言各写各的，
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

## ⚠️ MySQL 的两个坑（都与「静默」有关）

- **DO NOTHING 不能用 `INSERT IGNORE`**。它会把「列超长、违反 NOT NULL」这类错误
  一并降级成警告——用一个静默的写失败换一行代码。这里改用
  `ON DUPLICATE KEY UPDATE <pk> = <pk>`（把主键更新成自己），只吃掉唯一冲突。
- **MySQL 忽略冲突目标**。`ON CONFLICT (a, b)` 里那对列在 MySQL 上没有对应语法，
  任何唯一键冲突都会触发更新。所以本模块的契约是 **C-D1**：只允许用在
  「该表只有一个唯一约束」的表上。现有三处调用都已核对满足。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Insert, inspect
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.engine import Result
from sqlmodel import Session, SQLModel

# 各家的方言名。`mariadb` 与 `mysql` 的语法一致，共用一条分支。
_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})


def dialect_name(session: Session) -> str:
    """当前 `Session` 绑定的方言名（`sqlite` / `mysql` / `postgresql` …）。

    取 `get_bind()` 而不是读配置：测试里同一个进程会换库（每个用例一个临时 SQLite），
    读配置会拿到「注册时的那个」而不是「这条 Session 真正连的那个」。
    """
    return session.get_bind().dialect.name


def _insert_for(model: type[SQLModel], name: str) -> Any:
    """按方言取对应的 `Insert` 构造器。

    未知方言直接抛错，而不是回落到某个「看起来差不多」的实现：
    回落的后果是生成一条在目标库上语法错误的 SQL，错误信息会指向数据库而不是这里。
    """
    if name in _MYSQL_FAMILY:
        return mysql.insert(model)
    if name == "postgresql":
        return postgresql.insert(model)
    if name == "sqlite":
        return sqlite.insert(model)
    raise NotImplementedError(
        f"upsert 尚未适配方言 {name!r}；新增方言请在本模块加一条分支，"
        "不要在调用方写 if（那会把方言重新散开）。"
    )


def _primary_key_name(model: type[SQLModel]) -> str:
    """单列主键的列名。

    只支持单列主键：复合主键下「把主键更新成自己」要写多列，而这个项目里不存在
    复合主键（14 张表全部是 `id` 或单列业务键）。真出现时宁可报错，也不要猜一个。
    """
    keys = inspect(model).primary_key
    if len(keys) != 1:
        raise NotImplementedError(
            f"{model.__name__} 是复合主键；upsert 的 no-op 更新需要显式指定全部主键列。"
        )
    return keys[0].name


def _apply_upsert(
    stmt: Insert,
    *,
    name: str,
    model: type[SQLModel],
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None,
) -> Insert:
    """把「冲突怎么办」贴到一个已经构造好的 `Insert` 上。

    抽出来是因为 `upsert_values` 与 `upsert_select` 只在「行从哪来」上不同，
    冲突处理完全共享——两处各写一遍，迟早会有一处漏掉 MySQL 的 no-op 写法。
    """
    if name in _MYSQL_FAMILY:
        if update_cols is None:
            # 只吃掉唯一冲突：把主键更新成自己。见模块 docstring 的第一个坑。
            pk = _primary_key_name(model)
            return stmt.on_duplicate_key_update({pk: stmt.inserted[pk]})
        return stmt.on_duplicate_key_update(
            {col: stmt.inserted[col] for col in update_cols}
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
            model=model,
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
            model=model,
            conflict_cols=conflict_cols,
            update_cols=update_cols,
        )
    )


__all__ = ["dialect_name", "upsert_select", "upsert_values"]
