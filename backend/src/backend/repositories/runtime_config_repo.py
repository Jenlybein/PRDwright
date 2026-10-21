"""运行时配置的持久化：`system_config` 与 `model_providers`。

为什么单独一个仓储文件
----------------------
这两张表只被「配置层」读写：`core/runtime_config.py` 读、`services/admin_config_service.py`
写。把它们塞进 `admin_repo` 会让那个文件同时承担「用户/邀请码/统计/审计/配置」五种
职责，看不出边界。

两条纪律
--------
1. **SQL 只在这里**（`repositories/__init__.py` 的分层约定 + `tests/test_layering.py`）。
2. **列清单显式写出、不用 `SELECT *`**：将来给表加列时，不该静默改变调用方拿到的形状。
   这一条对 `model_providers` 尤其重要——它的返回值会直接进后台接口，
   `SELECT *` 意味着「加一个内部字段就等于对外多暴露一个字段」。

密钥的处理
----------
本文件**永远不存、不返回明文密钥**。`api_key_env_var` 只是「去哪个环境变量取」的指针
（ADR-042）。真正取值在 `core/secrets.py`，且只发生在构造 LLM 的前一刻。
"""

from __future__ import annotations

import json
from typing import Any

from backend.core.db import connect, utc_now_text

# `model_providers` 的显式列清单。凭证只暴露变量名，绝不暴露值。
_PROVIDER_COLUMNS = (
    "id, name, protocol, base_url, api_key_env_var, default_model, max_tokens, "
    "context_window, max_output, thinking_enabled, extra_json, builtin, enabled, "
    "sort_order, created_at, updated_at"
)


class SystemConfigRepository:
    """`system_config`：**只存覆盖项**，没有某一行 = 用 env 或默认值。"""

    @staticmethod
    def get_all() -> dict[str, str]:
        """读出全部覆盖项，返回 `key -> value(JSON 文本)`。

        value 保持 JSON 文本形态交给上层解码，而不是在这里 `json.loads`：
        解码要与「类型归一 + 约束校验」一起做（`runtime_config` 那侧），
        分两处解会让「一个坏值」在两个地方各有一套容错逻辑。
        """
        with connect() as conn:
            rows = conn.execute("SELECT key, value FROM system_config").fetchall()
            return {str(row["key"]): str(row["value"]) for row in rows}

    @staticmethod
    def upsert_many(items: dict[str, str], *, updated_by: int | None = None) -> None:
        """批量写入覆盖项（存在则覆盖）。

        整批在一个事务里：配置是**一组语义**，写一半会让「并发度改了、审核开关没改」
        这种撕裂状态被读到。要么全生效，要么全不生效。
        """
        if not items:
            return
        now = utc_now_text()
        with connect() as conn:
            conn.executemany(
                "INSERT INTO system_config (key, value, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at, "
                "updated_by = excluded.updated_by",
                [(key, value, now, updated_by) for key, value in items.items()],
            )
            conn.commit()

    @staticmethod
    def delete_many(keys: list[str]) -> int:
        """删除覆盖项（= 恢复 env 默认），返回删除行数。"""
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        with connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM system_config WHERE key IN ({placeholders})", tuple(keys)
            )
            conn.commit()
            return int(cursor.rowcount or 0)


class ModelProviderRepository:
    """`model_providers`：供应商登记与读取。"""

    @staticmethod
    def list_all() -> list[dict[str, Any]]:
        """全部供应商，按 `sort_order`、`id` 升序。

        不过滤 `enabled`：后台要能看见「已停用」的那些并重新启用；
        过滤由调用方（`runtime_config` 取激活项时）自己做。
        """
        with connect() as conn:
            rows = conn.execute(
                f"SELECT {_PROVIDER_COLUMNS} FROM model_providers "
                "ORDER BY sort_order, id"
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def get(provider_id: str) -> dict[str, Any] | None:
        """按 id 取一家；不存在返回 `None`（由调用方决定 404 还是降级）。"""
        with connect() as conn:
            row = conn.execute(
                f"SELECT {_PROVIDER_COLUMNS} FROM model_providers WHERE id = ?",
                (provider_id,),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def create(
        *,
        provider_id: str,
        name: str,
        protocol: str,
        base_url: str = "",
        api_key_env_var: str = "",
        default_model: str = "",
        max_tokens: int = 4096,
        context_window: int = 0,
        max_output: int = 0,
        thinking_enabled: bool = False,
        extra_json: str = "{}",
        sort_order: int = 100,
    ) -> None:
        """登记一家新供应商。`builtin` 恒为 0（内置行只由迁移播种）。

        id 冲突由调用方先查重并转成 400：这里让它直接抛 `sqlite3.IntegrityError`，
        是为了「检查与写入之间被并发插入」这条竞态也有兜底，而不是静默覆盖别人的行。
        """
        now = utc_now_text()
        with connect() as conn:
            conn.execute(
                "INSERT INTO model_providers "
                "(id, name, protocol, base_url, api_key_env_var, default_model, max_tokens, "
                " context_window, max_output, thinking_enabled, extra_json, builtin, enabled, "
                " sort_order, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?)",
                (
                    provider_id,
                    name,
                    protocol,
                    base_url,
                    api_key_env_var,
                    default_model,
                    int(max_tokens),
                    int(context_window),
                    int(max_output),
                    1 if thinking_enabled else 0,
                    extra_json or "{}",
                    int(sort_order),
                    now,
                    now,
                ),
            )
            conn.commit()

    @staticmethod
    def update(provider_id: str, fields: dict[str, Any]) -> bool:
        """按字段更新一家供应商，返回是否命中。

        `id` 与 `builtin` 不允许改（前者是身份、后者由迁移决定），
        即使调用方传了也会被这里过滤掉——规则写在一处，接口层不必各自记得。
        """
        allowed = {
            "name",
            "protocol",
            "base_url",
            "api_key_env_var",
            "default_model",
            "max_tokens",
            "context_window",
            "max_output",
            "thinking_enabled",
            "extra_json",
            "enabled",
            "sort_order",
        }
        patch = {key: value for key, value in fields.items() if key in allowed}
        if not patch:
            return False
        assignments = ", ".join(f"{key} = ?" for key in patch)
        params = [
            int(value) if isinstance(value, bool) else value for value in patch.values()
        ]
        with connect() as conn:
            cursor = conn.execute(
                f"UPDATE model_providers SET {assignments}, updated_at = ? WHERE id = ?",
                (*params, utc_now_text(), provider_id),
            )
            conn.commit()
            return int(cursor.rowcount or 0) > 0

    @staticmethod
    def delete(provider_id: str) -> bool:
        """删除一家供应商。内置行的拦截在服务层（这里不判断业务规则）。"""
        with connect() as conn:
            cursor = conn.execute(
                "DELETE FROM model_providers WHERE id = ?", (provider_id,)
            )
            conn.commit()
            return int(cursor.rowcount or 0) > 0

    @staticmethod
    def count() -> int:
        """行数。给迁移播种的幂等用例与「表是不是空的」判断用。"""
        with connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM model_providers").fetchone()
            return int(row["n"]) if row else 0


def decode_extra(raw: str | None) -> dict[str, Any]:
    """把 `extra_json` 解成对象；坏值给 `{}`。

    放在仓储模块而不是 `core/runtime_config`：它是「这一列怎么存」的知识，
    与表结构同源。坏值容忍与 `budget_step_caps` 的口径一致——一个坏字段
    不该让整份配置失效。
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
