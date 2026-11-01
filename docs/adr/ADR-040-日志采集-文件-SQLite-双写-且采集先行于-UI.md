# ADR-040：日志采集「文件 + SQLite 双写」，且采集先行于 UI

> 源：v4 层（原 `docs/architecture/v4/ADR.md`，已并入本目录）｜ [索引](./README.md)

### Status

Accepted（2026-10-21）

### Context

`core/logging_setup.py` 目前只装了一个 `StreamHandler(sys.stdout)`，
**没有任何持久化**：进程停止后历史全无，管理端也就无从查看。
日志格式本身已经很规整（ts / level / logger / msg / request_id / `extra=` 字段，
`LOG_FORMAT=json` 时一行一条 JSON），采集端几乎不需要改格式化逻辑。

候选方案：

1. **纯轮转文件**：简单，但后台检索要现解析文本、处理轮转与并发读；
2. **纯 SQLite 表**：查询灵活，但每次 `logger.info` 都写库，且丢了文件这个兜底；
3. **文件 + DB 双写**；
4. **交给外部采集**（Docker logs / ELK）：运维更专业，但与「自建小工具开箱即用」相悖，
   且用户明确要求后台内自带日志查看，必须自持。

### Decision

取 **方案 3（双写）**，并附加一条排期约束：

- 新增 `LogStoreHandler`，在 `configure_logging` 里与 stdout handler 并列装上，
  **业务代码一行都不用改**（与 `request_id` 自动注入同一条设计纪律）。
- **轮转文件始终开**（`logs/prdwright.log`，5MB × 5）：保证永不丢、可脱机排查、可外喂采集。
- **`app_logs` 表可开关**（默认开，异步批量写）：保证后台能过滤 / 分页 / 排序。
- **保留策略**：`log_retention_days`（默认 7 天）与 `log_max_rows`（默认 5 万条），
  启动时执行一次 + 写入时懒触发。
- ⚠️ **采集排在 T0，UI 排在 T4**：日志是「从现在开始攒」的数据，
  采集晚一步，后台上线那天就没有历史可看。
- 三条硬约束：绝不阻塞主流程（异步批量 + 队列上限 + 失败降级丢弃）；
  必须 `require_admin`；`log_db_min_level` 默认 INFO（DEBUG 不默认入 DB）。

### Consequences

**变容易**

- 后台能做级别 / 时间 / logger / event / `request_id` / 关键词的组合检索与分页。
- `request_id` 反查成为可能——用户回执上那个号粘进后台即可拉出该请求全部日志，
  把 R1 就贯通的追踪链补上最后一环。
- 文件与 DB 互为兜底：DB 出问题仍有文件，反之亦然。

**变难 / 代价**

- 多一份存储与保留策略要维护；日志写放大需要靠批量与级别下限控制。
- 日志可能含用户名、方案 id，属高权限数据，访问面必须收紧到管理员。

---
