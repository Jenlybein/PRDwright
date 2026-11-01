# ADR-041：系统配置「DB 覆盖 env，但只有白名单项可运行时改」

> 源：v4 层（原 `docs/architecture/v4/ADR.md`，已并入本目录）｜ [索引](./README.md)

### Status

Accepted（2026-10-21）

### Context

当前所有可调项都由 `.env` 经 `get_settings()` 读成**进程内 `@lru_cache` 单例**，
进程启动后不变。要做到「管理员在后台改参数并生效」，必须有运行时可变的配置源。

三个选项：

1. 只改 `.env` + 重启：运维负担重，且不符合「后台可调」的诉求；
2. **全部搬进 DB**：看似统一，但 `db_path` / `host` / `port` / `auth_enabled` /
   `cors_origins` 这类**启动项**一旦进 DB，就会出现「改了却不生效」或「改了要不要重启」
   的认知混乱，甚至改坏之后服务起不来；
3. **DB 覆盖 env，但只有白名单项可进 DB**。

### Decision

取 **方案 3**。

- `system_config` 表 + `core/runtime_config.py`；解析顺序 **DB > env > 字段默认值**。
- **白名单**：配额、模型与调用参数、安全与会话、**日志参数**、维护模式。
- **永不进 DB**（启动项）：`db_path`、`host`、`port`、`auth_enabled`、`cors_origins`、
  供应商密钥、邀请码种子、`init_admin_*`、`skills_strict`。
- 进程内 **30 秒 TTL** 缓存；管理员写入时**立即失效**并记审计（多进程靠 TTL 兜底，
  不引入消息广播）。
- 写入在 `runtime_config` 集中校验，约束与 `config.py` 的 `Field(ge=, le=)` 同源；
  非法值 400，不静默截断。

### Consequences

**变容易**

- 绝大多数目标参数（`llm_timeout` / `llm_max_retries` / `prd_pipeline_concurrency` /
  `default_llm_provider` …）在 `config.py` 里**已经存在**，把它们从「只读 env」改成
  「优先读 runtime_config」即可，UI 是纯增量——这是整个 v4 性价比最高的一块。
- 配置变更有审计（before / after），可追溯。

**变难 / 代价**

- 让 `llm_factory` / `job_service` 等改读 runtime_config 会**碰到生成主链路**。
  缓解：只改白名单项的读取点，且每改一处配一条「默认行为零变化」的回归用例。
- 白名单本身需要维护：新增可调项时要显式登记，忘了就还是只能改 env。

---
