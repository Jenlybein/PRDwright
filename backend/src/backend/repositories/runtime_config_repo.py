"""运行时配置的持久化：`system_config` 与 `model_providers`。

为什么单独一个仓储文件
----------------------
这两张表只被「配置层」读写：`core/runtime_config.py` 读、`services/admin_config_service.py`
写。把它们塞进 `admin_repo` 会让那个文件同时承担「用户/邀请码/统计/审计/配置」五种
职责，看不出边界。

两条纪律
--------
1. **事务边界不在这里**：两个类都绑定 `Session`，`commit()` 由 `session_scope()` 决定。
2. **返回整行对象、由调用方决定暴露哪些字段**：`system_config` 的读取只取 `key/value`
   （它是覆盖项表，别的列对上层无意义）；`model_providers` 返回整行，但**凭证只有密文**
   （`value_token`），后台视图自己决定要不要带出去。

密钥的处理
----------
本文件**只存、只返回密文**（`value_token`），永远不出现明文密钥。加密与解密都在
`core/crypto.py`，且解密只发生在构造 LLM 的前一刻（ADR-053）。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from backend.core.clock import utc_now_text
from backend.core.models import ModelProvider, SystemConfig


class SystemConfigRepository:
    """`system_config`：**只存覆盖项**，没有某一行 = 用 env 或默认值。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_all(self) -> dict[str, str]:
        """读出全部覆盖项，返回 `key -> value(JSON 文本)`。

        value 保持 JSON 文本形态交给上层解码，而不是在这里 `json.loads`：
        解码要与「类型归一 + 约束校验」一起做（`runtime_config` 那侧），
        分两处解会让「一个坏值」在两个地方各有一套容错逻辑。
        """
        rows = self.session.execute(
            select(SystemConfig.key, SystemConfig.value)
        ).all()
        return {str(row[0]): str(row[1]) for row in rows}

    def upsert_many(self, items: dict[str, str], *, updated_by: int | None = None) -> None:
        """批量写入覆盖项（存在则覆盖）。

        整批在一个事务里：配置是**一组语义**，写一半会让「并发度改了、审核开关没改」
        这种撕裂状态被读到。要么全生效，要么全不生效。

        `ON CONFLICT(key) DO UPDATE` 而不是「先删后插」：后者在并发下会短暂出现
        「这一行不存在」，而读配置是每个请求都要走的路径。
        """
        if not items:
            return
        now = utc_now_text()
        statement = sqlite_insert(SystemConfig).values(
            [
                {"key": key, "value": value, "updated_at": now, "updated_by": updated_by}
                for key, value in items.items()
            ]
        )
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "value": statement.excluded.value,
                    "updated_at": statement.excluded.updated_at,
                    "updated_by": statement.excluded.updated_by,
                },
            )
        )

    def delete_many(self, keys: list[str]) -> int:
        """删除覆盖项（= 恢复 env 默认），返回删除行数。"""
        if not keys:
            return 0
        result = self.session.execute(
            delete(SystemConfig).where(SystemConfig.key.in_(keys))
        )
        return int(result.rowcount or 0)


class ModelProviderRepository:
    """`model_providers`：模型配置条目（一条 = 一个 endpoint + 一套参数）。

    ⚠️ 这里的 `provider_id` 是**配置条目**的 id（`cfg_xxx`），不是协议名。
    同一协议可以有多行，所以任何「按协议取唯一一行」的假设都不成立。
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_all(self) -> list[ModelProvider]:
        """全部配置条目，按 `sort_order`、`created_at` 升序。

        不过滤 `enabled`：后台要能看见「已停用」的那些并重新启用；
        过滤由调用方（`runtime_config` 取激活项时）自己做。

        排序用 `created_at` 而不是 `id`：id 现在是随机后缀，按它排等于随机顺序，
        而「后加的在后面」才是管理员对列表的预期。
        """
        return list(self.session.execute(select(ModelProvider).order_by(
                    ModelProvider.sort_order, ModelProvider.created_at, ModelProvider.id
                )).scalars().all())

    def get(self, provider_id: str) -> ModelProvider | None:
        """按 id 取一条；不存在返回 `None`（由调用方决定 404 还是降级）。"""
        return self.session.get(ModelProvider, provider_id)

    def create(
        self,
        *,
        provider_id: str,
        name: str,
        protocol: str,
        base_url: str = "",
        value_token: str = "",
        default_model: str = "",
        max_tokens: int = 4096,
        context_window: int = 0,
        max_output: int = 0,
        thinking_enabled: bool = False,
        extra_json: str = "{}",
        sort_order: int | None = None,
    ) -> None:
        """登记一条新配置。

        `sort_order` 缺省时取「当前最大值 + 1」，让新加的一律排在末尾。
        不再写死 100：那样同批创建的条目顺序取决于 id（现在是随机串），
        管理员会看到列表顺序莫名跳动。

        id 冲突由调用方重试：这里让它直接抛 `IntegrityError`，
        是为了让「生成 id 与写入之间」也有兜底，而不是静默覆盖别人的行。
        注意需要 `flush()` 才会真的撞上约束——`add()` 只是把对象放进会话。
        """
        now = utc_now_text()
        if sort_order is None:
            current_max = self.session.execute(
                select(func.coalesce(func.max(ModelProvider.sort_order), 0))
            ).scalar_one()
            sort_order = int(current_max or 0) + 1
        self.session.add(
            ModelProvider(
                id=provider_id,
                name=name,
                protocol=protocol,
                base_url=base_url,
                value_token=value_token,
                default_model=default_model,
                max_tokens=int(max_tokens),
                context_window=int(context_window),
                max_output=int(max_output),
                thinking_enabled=bool(thinking_enabled),
                extra_json=extra_json or "{}",
                enabled=True,
                sort_order=int(sort_order),
                created_at=now,
                updated_at=now,
            )
        )
        # ⚠️ 必须 flush：调用方靠捕获 `IntegrityError` 来重试新 id，
        # 不 flush 的话冲突要到事务提交时才发现，那时已经出了重试循环。
        self.session.flush()

    def update(self, provider_id: str, fields: dict[str, Any]) -> bool:
        """按字段更新一条配置，返回是否命中。

        `id` / `builtin` / **`protocol`** 不允许改：前两者是身份与遗留标记，
        后者是这条配置的**本质**（换协议等于换一家厂商，参数语义全变：
        `max_tokens` 在 ark 与 anthropic 下的含义不同、`thinking_enabled`
        只有 ark 有语义）。规则写在这里，接口层与服务层都不必各自记得。
        """
        allowed = {
            "name",
            "base_url",
            "value_token",
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
        # 布尔列统一收成 0/1：调用方有的传 bool、有的传 int（表单解析结果），
        # 都在这里归一，免得 SQLAlchemy 对 `1` 与 `True` 的处理差异漏到上层。
        values = {key: int(value) if isinstance(value, bool) else value for key, value in patch.items()}
        values["updated_at"] = utc_now_text()

        result = self.session.execute(
            sa_update(ModelProvider)
            .where(ModelProvider.id == provider_id)
            .values(**values)
        )
        return int(result.rowcount or 0) > 0

    def delete(self, provider_id: str) -> bool:
        """删除一条配置。业务拦截（使用中的不能删）在服务层，这里不判断规则。"""
        result = self.session.execute(
            delete(ModelProvider).where(ModelProvider.id == provider_id)
        )
        return int(result.rowcount or 0) > 0

    def count(self) -> int:
        """行数。给迁移播种的幂等用例与「表是不是空的」判断用。"""
        return int(
            self.session.execute(select(func.count()).select_from(ModelProvider)).scalar_one()
        )


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
