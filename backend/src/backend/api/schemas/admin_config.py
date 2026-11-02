"""系统配置与模型供应商（v4 T3）的契约：可调项分组、供应商配置与密钥状态。

从 `backend.api.schemas` 拆出（#7）；对外引用由包的 `__init__` 再导出。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ============================================================================
# 系统配置与模型供应商（v4 T3）
# ============================================================================


class AdminConfigItem(BaseModel):
    """一个可调配置项。

    `value` 是**当前生效值**，`default` 是 env/代码默认值。两者不同说明后台覆盖过，
    `source` 会标成 `db`——这正是排查「改了没生效」时要看的第一列。
    """

    key: str = Field(description="配置键（白名单内）")
    value: Any = Field(description="当前生效值")
    default: Any = Field(description="环境变量 / 代码默认值；「恢复默认」后回到这个值")
    source: str = Field(description="`db`=后台覆盖过；`env`=用默认值")
    type: str = Field(description="控件类型：bool / number / enum / text")
    label: str = Field(description="中文名")
    help: str = Field(default="", description="这项是干什么的、改错了会怎样")
    danger: bool = Field(default=False, description="是否属于「改错了不明显但影响大」的危险项")
    options: list[str] | None = Field(default=None, description="枚举取值范围")
    min: float | None = Field(default=None, description="数值下界（含）")
    max: float | None = Field(default=None, description="数值上界（含）")
    min_exclusive: float | None = Field(default=None, description="数值下界（不含）")


class AdminConfigGroup(BaseModel):
    """一组配置项。分组是**产品定义**，由后端给，前端只负责渲染。"""

    key: str = Field(description="分组标识")
    label: str = Field(description="分组名")
    help: str = Field(default="", description="这一组整体在管什么")
    items: list[AdminConfigItem] = Field(default_factory=list, description="组内配置项")


class AdminActiveProvider(BaseModel):
    """当前激活供应商的只读视图（不含任何密钥）。"""

    id: str = Field(description="供应商 id")
    name: str = Field(description="展示名")
    protocol: str = Field(description="协议")
    protocol_label: str = Field(description="协议的中文说明")
    base_url: str = Field(default="", description="自定义地址；空=SDK 官方地址")
    default_model: str = Field(default="", description="默认模型名")
    max_tokens: int = Field(description="输出上限")
    context_window: int = Field(description="上下文窗口；0=用注册表")
    max_output: int = Field(description="预留输出；0=用注册表")
    thinking_enabled: bool = Field(default=False, description="是否开启思考（仅方舟有语义）")


class AdminConfigResponse(BaseModel):
    """配置页所需的全部数据，一次拿全。"""

    groups: list[AdminConfigGroup] = Field(default_factory=list, description="分组后的可调项")
    active_provider_id: str = Field(default="", description="当前激活的供应商 id")
    active_provider: AdminActiveProvider | None = Field(
        default=None, description="当前激活供应商的详情；表不可用时为 null"
    )
    config_source: str = Field(description="`db` / `env` / `degraded`（表不可用时的降级）")
    revision: str = Field(description="配置指纹；保存后应变化，未变说明没生效")
    missing_key: str = Field(default="", description="激活供应商缺密钥时的提示；不缺为空")


class AdminUpdateConfigRequest(BaseModel):
    """批量修改配置。**先全量校验再写**：有一项非法则整批不落库。"""

    updates: dict[str, Any] = Field(
        description="键 → 新值。键必须在白名单内，值要满足该字段的约束（见 GET 的 min/max/options）"
    )


class AdminResetConfigRequest(BaseModel):
    """恢复默认（删除覆盖项）。"""

    keys: list[str] | None = Field(
        default=None, description="要恢复的键；不传或空数组表示全部恢复"
    )


class AdminProviderItem(BaseModel):
    """一条模型配置。

    ⚠️ **没有任何密钥字段**：`key_present` 只回布尔，密钥值永远不出后端。

    `id` 是**配置条目**的身份（`cfg_xxx`），不是供应商/协议名——同一协议可以有多条。
    """

    id: str = Field(description="配置条目 id（cfg_ 开头，后端生成，创建后不可改）")
    name: str = Field(description="配置名（给人看的标签，允许重名）")
    protocol: str = Field(description="协议：openai_compatible / anthropic / ark / google_genai / nvidia / huggingface")
    protocol_label: str = Field(description="协议的中文说明")
    base_url: str = Field(default="", description="自定义地址；空=SDK 官方地址")
    key_present: bool = Field(default=False, description="是否已有可用密钥（只回布尔）")
    default_model: str = Field(default="", description="默认模型名")
    max_tokens: int = Field(description="输出上限")
    context_window: int = Field(description="上下文窗口；0=用注册表默认值")
    max_output: int = Field(description="预留输出；0=用注册表默认值")
    thinking_enabled: bool = Field(default=False, description="是否开启思考（仅方舟有语义）")
    enabled: bool = Field(default=True, description="是否启用；停用后不能被设为使用中")
    sort_order: int = Field(default=0, description="列表排序")
    active: bool = Field(default=False, description="是否是当前使用中的那一条")
    extra: dict[str, Any] = Field(default_factory=dict, description="其他协议参数")


class AdminProviderPreset(BaseModel):
    """新增配置时的供应商模板（选一家自动填好协议、地址与变量名）。

    ⚠️ 地址会变，以官方文档为准；它只是**模板**，填完仍可改。

    `key` 只是下拉框的 value，**不是**配置 id——配置 id 由后端生成。
    """

    key: str = Field(description="模板标识（下拉框用；不是配置 id）")
    name: str = Field(description="供应商展示名")
    protocol: str = Field(description="协议；选定后该配置的协议即确定，创建后不可改")
    base_url: str = Field(description="建议的接口地址")
    default_model: str = Field(default="", description="建议的模型名")


class AdminProtocolOption(BaseModel):
    """可选的协议。"""

    value: str = Field(description="协议标识")
    label: str = Field(description="中文说明与覆盖范围")


class AdminProviderListResponse(BaseModel):
    """配置列表 + 新增所需的选择项。"""

    providers: list[AdminProviderItem] = Field(default_factory=list, description="全部配置条目")
    active_provider_id: str = Field(default="", description="当前使用中的那条配置 id")
    presets: list[AdminProviderPreset] = Field(default_factory=list, description="新增时的供应商模板")
    protocols: list[AdminProtocolOption] = Field(default_factory=list, description="可选的协议")
    warning: str = Field(default="", description="切换成功但有风险时的提示（如缺密钥）")


class AdminCreateProviderRequest(BaseModel):
    """新增一条配置。

    ⚠️ **没有 `id`**：它由后端生成（`cfg_xxx`）。同一协议可以有多条配置，
    让管理员手填 id 只会撞名。
    """

    name: str = Field(min_length=1, max_length=64, description="配置名，如「方舟-生产」；必填、允许重名")
    protocol: str = Field(description="协议（见 /providers 的 protocols）；创建后不可改")
    base_url: str = Field(default="", description="接口地址；OpenAI 兼容类通常要带 /v1")
    api_key: str | None = Field(
        default=None,
        description=(
            "密钥明文（只用于本次加密入库，响应里不会出现）。"
            "不传/空串=这条配置暂不配密钥（本地 vLLM 可填占位值 not-needed）"
        ),
    )
    default_model: str = Field(default="", description="默认模型名")
    max_tokens: int = Field(default=4096, ge=1, description="输出上限")
    context_window: int = Field(default=0, ge=0, description="上下文窗口；0=用注册表（自定义模型建议填）")
    max_output: int = Field(default=0, ge=0, description="预留输出；0=用注册表")
    thinking_enabled: bool = Field(default=False, description="是否开启思考（仅方舟有语义）")
    extra: dict[str, Any] = Field(default_factory=dict, description="其他协议参数")
    sort_order: int | None = Field(default=None, description="列表排序；不传=排到末尾")


class AdminUpdateProviderRequest(BaseModel):
    """编辑一条配置。只传要改的字段；`id` 不可改。

    `protocol` 字段**保留**但只允许「与原值相同」：服务层对不同的值显式报 400。
    删掉这个字段看似更干净，实际更糟——旧客户端发 `protocol` 会得到「200 但没改」，
    比一句明确的报错难查得多。
    """

    name: str | None = Field(default=None, description="配置名")
    protocol: str | None = Field(default=None, description="协议；创建后不可改（换了会 400）")
    base_url: str | None = Field(default=None, description="接口地址")
    api_key: str | None = Field(
        default=None, description="新密钥明文；传了才会重加密覆盖（不传=保持不变）"
    )
    clear_key: bool | None = Field(default=None, description="传 true 清空密钥（= 未配置）")
    default_model: str | None = Field(default=None, description="默认模型名")
    max_tokens: int | None = Field(default=None, ge=1, description="输出上限")
    context_window: int | None = Field(default=None, ge=0, description="上下文窗口")
    max_output: int | None = Field(default=None, ge=0, description="预留输出")
    thinking_enabled: bool | None = Field(default=None, description="是否开启思考")
    enabled: bool | None = Field(default=None, description="是否启用")
    extra: dict[str, Any] | None = Field(default=None, description="其他协议参数")
    sort_order: int | None = Field(default=None, description="列表排序")


class AdminProviderKeyResponse(BaseModel):
    """查看密钥明文的响应（一次性；每次查看都会写审计）。"""

    api_key: str = Field(description="解密后的密钥明文。前端应只用于本地展示/复制，不要缓存")


class AdminProviderTestResponse(BaseModel):
    """连通性测试结果。

    它背后真的发了一次最小调用（`max_tokens=16`、15 秒超时）——因为
    「参数填对了」与「真的能生成」是两件事：`base_url` 少了 `/v1`、密钥是别的
    环境的、协议选错了，这些在保存时都看不出来。
    """

    ok: bool = Field(description="是否连通")
    detail: str = Field(description="结果说明；失败时是具体原因")
    latency_ms: int = Field(default=0, description="本次调用的耗时（毫秒）")
