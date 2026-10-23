# T14 · 接口性能监测面板（管理员端）

> **一句话**：让管理员在后台直接看到「哪个接口慢、错多少、数据到底可信到什么程度」——
> 复用已有的访问日志做纯查询，**不新增采集、不新增一次写放大**。
>
> 上层：`README.md`（本层索引）、`roadmap.md` 的 T14 节（排期与验收）、`ADR.md`（决策）
> 关联：`ARCHITECTURE.md` 第 7 节（SSE 传输契约）——本面板正是要把
> `duration_ms` 对 SSE 的语义讲清楚的那一页。

---

## 1. 目标与范围

### 1.1 三句话目标

1. **看得见**：不必再去日志页一条条翻，就能看到按接口聚合的耗时分布与错误率。
2. **不骗人**：`duration_ms` 对 SSE 只是建流耗时、日志窗口受保留策略约束、队列满会丢日志 ——
   这三件事必须让读者在不读源码的前提下不会误读。
3. **不添债**：不新增采集、不新增表、不给日志写入路径加常驻成本。

### 1.2 本期范围（In）

| # | 内容 |
| --- | --- |
| I1 | `GET /api/admin/metrics/endpoints`：按 `path` + `method` 聚合，出分位数、错误率、覆盖信息 |
| I2 | 普通接口与流式接口**分开两张表**，流式表不出现耗时数字 |
| I3 | 数据窗口卡：实际覆盖区间、行数、是否被保留策略截断、落库开关状态、丢弃下界 |
| I4 | 样本不足单列区：不给分位数、不参与排行 |
| I5 | `frontend_admin` 新增 `/performance` 页与侧栏导航项 |
| I6 | SSE 路径清单守卫测试（防止流式接口混进耗时排行） |

### 1.3 本期不做（Out）

| 不做 | 理由 |
| --- | --- |
| 实时推送 / SSE | 低频手动查看，刷新 + 切窗口足够。更重要的是：**SSE 正是本页要解释的那个陷阱的源头**，在讲陷阱的页面上再用一条 SSE 通道，是把解释成本和实现成本叠在一起 |
| 时间序列曲线 / `bucket` 分桶 | 时间趋势已有 `/api/admin/stats/daily`，再做一条就是两个入口看同一件事 |
| 历史指标快照（新聚合表 + 定时任务） | 日志本身已是 7 天滚动窗口；另存一份 = 双份保留策略 + 双份裁剪逻辑。真需要长期趋势，应先问「是不是该延长 `log_retention_days`」 |
| 告警阈值 / 通知 | 无通知渠道；做一半的告警比没有更危险 |
| 分布直方图 / 火焰图 | 直方图要额外分桶聚合，信息量低于「排行 + 分位数」；火焰图需要 tracing 基础设施，仓库里没有 |
| 按用户 / IP 排行 | `user_id` 在 `_row()` 里是「尽力带出，带不出就 NULL」，覆盖率不完整；按有空值的列排行，榜首会是一堆 `NULL` 归并成的假组 |
| 新增 `app_logs` 列 | `extra_json` 就是设计好的扩展位，`logging_setup._row()` 的注释明说「不为它们各开一列」 |
| 采样 | 量小（≤5 万行），采样只会让分位数失真 |

---

## 2. 口径速查表

**实现与评审逐条核对。每一条都是一次「最容易做错、且做错了不会报错」的判定。**

| # | 判定 | 正确做法 | 做错的样子 |
| --- | --- | --- | --- |
| 1 | `duration_ms` 对 SSE 的语义 | 只是**建流**耗时。流式接口单独成表，字段名 `setup_*_ms`，表里**不出现任何耗时数字**，只给「见任务监控 →」链接 | 流式接口带着 40ms 混进耗时排行，读者以为一次 3 分钟的生成本来就这么快 |
| 2 | 错误率 | **拆两个字段**：`server_error_rate`（5xx）与 `client_error_rate`（4xx）。**只有 5xx 染色** | 合并成 `status >= 400` —— 登录页输错密码（401）被报成故障 |
| 3 | p50 门槛 | `n ≥ 5`，否则返回 **`null`** | n=2 也出一个「中位数」 |
| 4 | p95 门槛 | `n ≥ 20`，否则返回 **`null`** | n=2 时 p95 其实就是 max，却叫它 p95 |
| 5 | p99 门槛 | `n ≥ 100`，否则返回 **`null`** | n<100 时 `ceil(n×0.99) ≡ n`，p99 恒等于 max |
| 6 | 不达标时返回什么 | **`null`** + 可读原因（如 `"n < 100"`） | 返回 `0` 或 `-1` —— 会渲染成一个看起来正常的数字 |
| 7 | 排行收录条件 | 只收 **`n ≥ 20`** 的组 | 让只调过 2 次的冷门接口霸榜，高频接口的真实劣化被挤下去 |
| 8 | `dropped` 的语义 | **下界 + 稀疏打点**（累计量，每约 100 条打一次）。`≥2` 条事件取 `MAX-MIN`；**1 条返 `null` 不返 `0`**；文案写「**至少**丢弃 N 条（粒度约 100）」 | ① 求和 —— `SUM` 会**高估**（事件累计 101/201/301 求和 604，实际 301）；② 1 条时返 `0`（坏消息显示成好消息）；③ 写「共丢弃 N 条」 |
| 9 | 空态 | **五种**，按 `coverage` 分派；其中「没有慢接口」用**成功绿** | 四种合成一个灰框 —— 分不清「系统健康」与「监控没跑」，而这两者的后续动作完全相反 |
| 10 | 时间窗被截断 | `truncated_by_retention` 为真时在顶部提示「实际只覆盖 {oldest} 起」 | 让读者以为「7 天前的数据是 0」 |

---

## 3. 事实核对

全部实测核对，无一处偏差。

| 事实 | 核对结果 |
| --- | --- |
| 访问日志写 `duration_ms` / `method` / `path` / `status`，`event='request.completed'` | 正确，`api/deps.py::_log_request()` |
| `app_logs` 十列结构 | 正确，`core/db.py:153-164` |
| 只有三个索引 | 正确，`db.py:167-171`：`idx_app_logs_ts(ts)`、`idx_app_logs_level(level, ts)`、`idx_app_logs_request(request_id)` |
| SQLite JSON1 可用 | 正确，`json_extract` 正常 |
| 迁移已到 v20，新迁移从 v21 起 | 正确 |
| `duration_ms` 语义是「收到请求 → 开始回响应」 | 正确。`_log_request` docstring 原文即写明，并明确说「否则看日志的人会以为一次 PRD 生成只花了 40 毫秒」 |

三条补充事实，直接影响设计：

1. **`method` / `path` / `status` / `duration_ms` 都在 `extra_json` 里，不是独立列。**
   `logging_setup._row()` 只把 `event` 与 `user_id` 提升成列，其余原样进 `extra_json`
   （注释明说「业务字段会不断新增，表结构不该跟着每次新增都迁移一次」）。所以聚合必须走 `json_extract`。
2. **日志会丢。** `log_db_queue_size` 默认 5000，满了**直接丢弃新日志**并记一条
   `event='log.db_backlog_dropped'`（带 `dropped` 累计数）。所以「库里有多少行」是**下界**。
3. **两个保留闸门独立生效**：`log_retention_days=7` 与 `log_max_rows=50000`，谁先触发谁生效；
   `log_db_enabled` 默认 True。

分层落点：已有 `repositories/log_repo.py`（`insert_many` / `prune` / `count` / `search`）与
`services/admin_service.py::list_logs`，新代码照这条链走，**不新建层**。

---

## 4. 接口契约

`GET /api/admin/metrics/endpoints`（`require_admin`）

### 4.1 请求参数

| 参数 | 类型 | 默认 | 取值 | 说明 |
| --- | --- | --- | --- | --- |
| `since` | ISO8601 | `until - 24h` | 任意 | 起始时间（UTC） |
| `until` | ISO8601 | 现在 | 任意 | 结束时间 |
| `top` | int | 15 | 1–50 | 每个表返回多少行 |
| `slow_ms` | float | 800 | 1–60000 | 「慢」的判定线，**只用于计数，不用于排行** |
| `include_stream` | bool | true | — | 关掉就只查普通接口 |

> 没有 `bucket`：本期只出「窗口内聚合排行」，不出时间序列。理由见 1.3。

### 4.2 响应结构

```json
{
  "window": { "since": "2026-10-22T06:59:00.000Z", "until": "2026-10-23T06:59:00.000Z" },
  "coverage": {
    "log_db_enabled": true,
    "oldest_ts": "2026-10-18T02:11:07.412Z",
    "newest_ts": "2026-10-23T06:58:51.003Z",
    "total_rows": 41112,
    "window_request_rows": 1204,
    "retention_days": 7,
    "max_rows": 50000,
    "truncated_by_retention": true,
    "truncated_by_max_rows": false,
    "dropped_by_queue_at_least": 0
  },
  "thresholds": { "slow_ms": 800.0, "min_n_p50": 5, "min_n_p95": 20, "min_n_p99": 100 },
  "http_endpoints": [
    {
      "method": "GET",
      "path": "/api/admin/stats",
      "count": 42,
      "p50_ms": 38.0,
      "p95_ms": 210.5,
      "p99_ms": null,
      "max_ms": 512.0,
      "server_error_rate": 0.0,
      "client_error_rate": 0.0714,
      "slow_count": 1,
      "p99_unavailable_reason": "n < 100"
    }
  ],
  "stream_endpoints": [
    {
      "method": "POST",
      "path": "/api/conversation/generate-prd-stream",
      "count": 17,
      "setup_p50_ms": 41.2,
      "setup_p95_ms": 88.0,
      "server_error_rate": 0.0,
      "client_error_rate": 0.0
    }
  ],
  "insufficient_samples": [
    { "method": "GET", "path": "/api/health", "count": 2, "reason": "n < 5，连中位数都不成立" }
  ],
  "elapsed_ms": 87.4
}
```

### 4.3 字段口径

| 字段 | 单位 | 口径 |
| --- | --- | --- |
| `coverage.total_rows` | 行 | `app_logs` 全表行数（不限窗口），用于判断「有没有数据」 |
| `coverage.window_request_rows` | 行 | 窗口内 `event='request.completed'` 行数，用于区分「窗口空」与「表空」 |
| `coverage.oldest_ts` / `newest_ts` | UTC | 全表最早/最晚，用于说明「数据实际只覆盖到这里」 |
| `truncated_by_retention` | bool | `oldest_ts > since` ⇒ 窗口起点早于数据起点 |
| `truncated_by_max_rows` | bool | `total_rows >= max_rows` ⇒ 已达行数上限，正在滚动淘汰 |
| `dropped_by_queue_at_least` | 条 / null | 按窗口内事件数分派（详见 6.3）：0 条 → `0`；1 条 → `null`；≥2 条 → `MAX(dropped) - MIN(dropped)`。**下界，粒度约 100 条** |
| `server_error_rate` | 0–1 | `5xx / count` |
| `client_error_rate` | 0–1 | `4xx / count` |
| `stream_endpoints[].setup_p50_ms` | ms | **建流耗时**，字段名刻意不叫 `p50_ms` |
| `elapsed_ms` | ms | 本次查询自身耗时。**它让「要不要加索引」变成数据问题**（见 6.2） |

两处刻意的命名选择，都是为了不让人误读：

- 流式接口的耗时字段叫 `setup_*_ms`，不叫 `p50_ms` —— 字段名本身就说清了它是建流。
- 错误率拆成两个字段，**没有**笼统的 `error_rate`。理由见 5.1。

---

## 5. SQL 与聚合思路

### 5.1 错误率必须拆 4xx / 5xx

`deps.py` 的 docstring 明确写「记在最外层中间件里，所以**被认证拦掉的 401 也有这一条**」，
并举了「用户说打不开」时要先确认请求到没到过后端。**也就是说 401 / 403 是被有意记录的正常流量。**

若按 `status >= 400` 算「错误率」：

- 登录页每输错一次密码 → 401 → 错误率上升 → 面板把**正常的鉴权拒绝报成故障**；
- 500（真故障）与 401（正常拒绝）在同一个分子里，权重相同。

所以拆两个字段，且**告警色只跟 5xx 走**，4xx 作为参考量平铺显示、不染色。

### 5.2 分位数：`ROW_NUMBER()` + 最近秩法

| 方案 | 取舍 | 结论 |
| --- | --- | --- |
| `NTILE(100)` | n<100 时桶为空或每桶不足一行，p95 直接退化；等分桶的边界不对齐你要的百分位 | 否决 |
| `PERCENTILE_CONT` / `PERCENTILE_DISC` | SQLite 没有这两个函数 | 否决 |
| `ROW_NUMBER()` + 取第 ⌈n·p⌉ 行 | 精确、任意 n 都成立、与延迟 SLO 的通行口径一致（它回答「最慢那 5% 从多少毫秒起步」） | **采用** |

```sql
WITH win AS (
  SELECT
    json_extract(extra_json, '$.method')                       AS method,
    json_extract(extra_json, '$.path')                         AS path,
    CAST(json_extract(extra_json, '$.status')      AS INTEGER) AS status,
    CAST(json_extract(extra_json, '$.duration_ms') AS REAL)    AS dur
  FROM app_logs
  WHERE event = 'request.completed'
    AND ts >= :since AND ts <= :until
    AND json_extract(extra_json, '$.duration_ms') IS NOT NULL
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY path, method ORDER BY dur) AS rn,
         COUNT(*)     OVER (PARTITION BY path, method)              AS n
  FROM win
)
SELECT
  method, path, n,
  AVG(dur) AS avg_ms,
  MAX(dur) AS max_ms,
  MAX(CASE WHEN rn = (n * 50 + 99) / 100 THEN dur END) AS p50_ms,
  MAX(CASE WHEN rn = (n * 95 + 99) / 100 THEN dur END) AS p95_ms,
  MAX(CASE WHEN rn = (n * 99 + 99) / 100 THEN dur END) AS p99_ms,
  SUM(CASE WHEN status >= 500              THEN 1 ELSE 0 END) AS n_5xx,
  SUM(CASE WHEN status BETWEEN 400 AND 499 THEN 1 ELSE 0 END) AS n_4xx,
  SUM(CASE WHEN dur >= :slow_ms            THEN 1 ELSE 0 END) AS slow_count
FROM ranked
GROUP BY path, method
ORDER BY p95_ms DESC;
```

两个实现细节：

- **`ceil` 用整数除法实现**：`(n*95 + 99) / 100`。SQLite 的 `/` 在整数间是截断除法，
  该式正好给出 `ceil(n*0.95)`，不依赖 `ceil()` 是否存在（3.35+ 才有）。
  验算：n=100 → (9500+99)/100 = 95；n=2 → (190+99)/100 = 2（第 2 个，即最大值）。
- **`n` 是分组内常量**，写进 `MAX(CASE WHEN ...)` 是安全的（SQLite 允许非聚合列出现在聚合
  表达式里，任取一行结果相同）。想要更干净，可在 `ranked` 里预计算 `rn50` / `rn95` / `rn99` 再外层取。

组装顺序：`log_repo` 返回分组明细 → `admin_service` 算比率、套阈值、按流式/普通分流、拼 `coverage`。

### 5.3 小样本：三档硬门槛

| 统计量 | 最小 n | 为什么不能再低 |
| --- | --- | --- |
| p50 | 5 | n<5 时中位数只是两三个数的中间值，任何一次抖动就改写它 |
| p95 | 20 | n=20 时 `ceil(20×0.95)=19`，取的是第 19/20 个，**已紧贴极值**；n 更小时直接退化成 `max` |
| p99 | 100 | n<100 时 `ceil(n×0.99) ≡ n`，**p99 恒等于 max**；叫它 p99 会让人以为「99% 都在这以内」 |

**这三档是硬门槛，不是显示优化。** 具体做法：

1. 不达门槛返回 `null` + 可读原因，**绝不返回 `0` / `-1`**。
2. **排行只收 `n ≥ 20` 的组。** 否则榜首永远是冷门接口（它们的最小样本里偶然有一次慢请求），
   而高频接口上稳定的劣化被挤下去。
3. `n < 5` 的组整组移入 `insufficient_samples`，只给 `count` 与原因，**不给任何分位数**。
4. 前端把 n 直接显示在次数列，**不藏进 tooltip** —— n 是判断整张表可不可信的第一手信息。

---

## 6. 索引评估

### 6.1 结论：v21 不加索引

**① `ts` 范围过滤已有索引，这一刀砍得最大。** `idx_app_logs_ts(ts)` 覆盖
`ts >= :since AND ts <= :until`，把扫描范围从全表收窄到窗口。这是最有价值的一步，且已经在。

**② `extra_json` 无法索引，但量级可控。** `event='request.completed'` 与 `json_extract(...)`
退化成「在已收窄的行上逐行解析 JSON」。但 `log_max_rows` 硬上限 50000 行 —— 窗口扫描是万级行，
JSON 解析万级小文档在 SQLite 上是几十到几百毫秒。对**管理员手动打开**的看板可接受。

**③ 真正的反对理由在写路径，不在读路径。** 日志写入是「异步批量 + 队列满即丢」
（`log_db_queue_size=5000`），项目对它的硬要求是「绝不阻塞主流程」。给 `app_logs` 加
**表达式索引**（`json_extract(extra_json,'$.path')`）或**生成列索引**，会让**每一条日志的
INSERT** 都多一次 JSON 解析 + 索引维护。

> 为了一个手动查看的面板，去加重整个应用唯一的日志旁路 —— 代价不对等。
>
> 而且这条旁路已被设计成「可以牺牲的那一半」（`log_db_enabled` 的 description 原文：
> 「落库是可被牺牲的那一半，文件不是」）。在可牺牲的一侧加常驻写成本，方向是反的。

### 6.2 将来若测出来慢：按这个阶梯加，不要跳步

| 顺序 | 动作 | 写入代价 | 何时才做 |
| --- | --- | --- | --- |
| 0 | **先量**（响应里已带 `elapsed_ms` 与 `window_request_rows`） | 0 | 现在就做 |
| 1 | **部分索引**：`CREATE INDEX IF NOT EXISTS idx_app_logs_req_ts ON app_logs(ts) WHERE event = 'request.completed';` | 近 0（索引里只有 `ts`，插入时多一次字符串比较，**不含 JSON**） | `elapsed_ms > 1000` 且 `window_request_rows` 接近 5 万 |
| 2 | `extra_json` 表达式索引或生成列 | **高**（每次 INSERT 解析 JSON） | 第 1 步之后仍不达标 |
| 3 | 提高 `log_max_rows` 之前必须回到第 2 步重新评估 | — | — |

第 1 步是第一根杠杆：它把扫描面从「全窗口所有日志」缩到「全窗口的请求日志」
（请求日志只是其中一类，生成过程的逐章日志通常多得多），而写入代价几乎为零。

> `elapsed_ms` 进响应的意义就在这里：让「要不要加索引」以后能用数据回答，而不是靠谁猜。

### 6.3 丢弃数的口径：`SUM` 是错的，必须用 `MAX - MIN`

`LogStoreHandler` 的两个事实决定了这里的算法（`core/logging_setup.py:279/296/299/304`）：

```python
self._dropped = 0                            # 279：累计计数器
self._dropped += 1                           # 296：只增不减
if self._dropped % 100 == 1:                 # 299：只在 1, 101, 201, 301… 打点
    ... extra={"dropped": self._dropped}     # 304：存的是当时的累计数
```

1. 事件里存的是**累计值**，不是「本次丢了多少」；
2. 打点是**稀疏的** —— 每约 100 条才记一次。

所以 **`SUM` 会高估**：三件事件（累计 101 / 201 / 301）求和 = 604，而实际只丢了 301 ——
高估近一倍，且事件越多偏得越远。

**三档口径，按窗口内事件数分派：**

| 窗口内事件数 | 取值 | 文案 |
| --- | --- | --- |
| **0 条** | `0` | 「无丢弃」—— **这是可信的真零**（没溢出就不会打点） |
| **1 条** | `null` | 「有丢弃记录，但窗口内不足以估算」 |
| **≥2 条** | `MAX(dropped) - MIN(dropped)` | 「至少丢弃 N 条（记录粒度约 100 条）」 |

三条理由：

- **`MAX - MIN` 才是窗口内的增量**：`MAX` 是最后一次打点时的累计数、`MIN` 是第一次的，
  相减抵掉了窗口之前的历史。直接取 `MAX` 会把开服以来的总量算进这个窗口。
- **1 条时必须返回 `null` 而不是 `0`**：只有一条打点时 `MAX - MIN = 0`，但实际可能已丢过
  99 条。返回 `0` 会被读成「没有丢弃」—— 这是最坏的一种错，因为它是**坏消息被显示成好消息**。
  与「没有慢接口」必须用成功绿是同一条原则：`0` 与 `null` 在这个字段上绝不能互换。
- **必须写出「粒度约 100 条」**：打点间隔约 100 条，所以「至少 100 条」与「至少 199 条」在库里
  长得一样。不写粒度，这个数字看起来比它实际的精度高一个数量级。

> 不建议写成 `MAX - MIN + 1` 去「补那一条」：粒度是 100，±1 毫无意义，反而让人以为它精确到条。
> 精度靠文案说清，不靠公式假装。

这类把下界说成真值的小错，会让整块看板一起失去可信度 —— 而这块看板存在的意义
恰恰是让人相信它给的数字。

---

## 7. 前端页结构

### 7.1 路由与导航

- 路由：`/performance`，name `performance`，`meta.requiresAdmin: true`，
  `component: () => import('@/views/PerformanceView.vue')`（懒加载，与既有页一致）。
- 导航：加到 `nav.ts` 的 `sys` 组，**排在 `logs` 之后**：
  `{ name: 'performance', label: '接口性能', code: 'LATENCY' }`
  理由：它和「系统日志」同类（排障视角的只读页），放一起比塞进 `ops` 组更顺。

### 7.2 五块内容

**① `PageHeader`**：`kicker`「Performance / 接口」，`title`「接口性能」；
`actions` 槽放时间窗切换（24 小时 / 7 天）+ 刷新；`desc` 槽一句话说清 `duration_ms` 定义。

**② 常驻口径带（关键，不是小字）**

一条整宽的 `.brut` 带，紧贴页头下方，**不折叠、不缩小字号**：

> 本页的耗时是「收到请求 → 开始回响应」。流式接口的**生成时长不在这里** ——
> 一次 PRD 生成可能要 3 分钟，这里只记 40 毫秒。真实生成耗时请到任务监控看。
> 　[　去任务监控　]

三条让「不只是加一句小字」成立的做法：

1. 它占**独立一块**，有边框与底色，和内容卡同级；
2. 它**带跳转按钮**指向 `/jobs`（那里有 `run_summary` 的口径）——给了出口，用户才不会拿着 40 毫秒下结论；
3. **流式接口单独一张表**，表里根本不给叫「耗时」的列 —— 见 ④。
   数字带上误导性标签，加多少小字都救不回来；**不给那个数字，才是不误读的保证。**

**③ 数据窗口卡**（把 `coverage` 摊成四个等宽硬边小块）

| 块 | 内容 | 状态色 |
| --- | --- | --- |
| 数据范围 | `oldest_ts` ~ `newest_ts`（本地时间，日期粒度） | 若 `truncated_by_retention` → 警示黄 |
| 日志行数 | `total_rows / max_rows`（如 `41,112 / 50,000`） | 若 `truncated_by_max_rows` → 警示黄 |
| 窗口内请求 | `window_request_rows` | 中性 |
| 落库状态 | 开启 / 关闭；开启时附「至少丢弃 N 条」（有丢弃才显示） | 关闭 → 警示黄；有丢弃 → 警示黄 |

**④ 两张表**

普通接口表（按 p95 降序，只含 `n ≥ 20`）：

| 列 | 说明 |
| --- | --- |
| 方法 | `GET` / `POST`，等宽小字 |
| 路径 | `font-mono`，过长中间省略，悬停出全文 |
| 次数 | n，**直接显示不隐藏** |
| p50 / p95 / p99 | 右对齐；`null` 显示 `—`，悬停给原因 |
| 5xx 率 | **唯一染色的比率列** |
| 4xx 次数 | 纯数字，不染色 |
| 慢请求 | `slow_count`（≥ `slow_ms` 的条数） |

流式接口表（单独一张，标题下方即口径提示）：

| 列 | 说明 |
| --- | --- |
| 方法 / 路径 / 次数 | 同上 |
| **建流**（p50 / p95） | 列头就写「建流」，不叫「耗时」 |
| 5xx 率 / 4xx 次数 | 同上 |
| 生成耗时 | **不是数字**，固定显示「见任务监控 →」链接 |

最后那一列是「不给数字」的具体实现：流式行里**任何一个像耗时的数字都不出现**，只出现一个出口。

**⑤ 样本不足区**：只两列（路径 / 原因，如「n=2 < 5，连中位数都不成立」）。
不给分位数、不参与排序，并明确标注「数据不足，**不代表这些接口快**」——
否则读者会默认「没进排行的就是好的」，那正好是反的。

### 7.3 五种空态（其中一态是好消息）

空态不能只有一种，必须按 `coverage` 分派：

| # | 判定条件 | 显示 | 语气 |
| --- | --- | --- | --- |
| 1 | `log_db_enabled == false` | 「日志落库已关闭 —— 本页没有任何数据可算」+「去配置页开启」 | **警示黄**（配置选择，不是故障） |
| 2 | `total_rows == 0` | 「还没有任何请求日志」（刚部署 / 刚开开关） | 中性 |
| 3 | `window_request_rows == 0` 且 `total_rows > 0` | 「这个时间窗内没有接口请求。当前数据实际覆盖 {oldest} ~ {newest}」+「切到 7 天」 | 中性 |
| 4 | `window_request_rows > 0` 但排行表无行（全部 < `n ≥ 20`） | 「窗口内 {N} 次请求，样本都不足以算分位数」 | 中性 |
| 5 | 有行且 `slow_count == 0` | 「**{N} 次请求，没有慢接口**」+ 成功色勾标 | **成功绿（正面）** |

第 5 态是要害：**「没有慢请求」是好消息，绝不能和「没有数据」共用同一套灰色空态。**
四态合一的结果是运维看到灰框，分不清「系统很健康」还是「监控根本没跑」——
而这两种判断的后续动作完全相反。

`truncated_by_retention` 为真时另在顶部加提示：「时间窗早于日志保留范围，本页实际只覆盖
{oldest} 起」。默认 7 天保留 + 面板默认 24 小时不会触发，但用户切到 7 天时正好卡在边界，
不说清就会以为「7 天前的数据是 0」。

### 7.4 视觉约束（Sky Brutalism）

- 只用既有令牌与类：`page-head` / `brut-card` / `brut-card-lg` / `.dark` 覆盖，**不新造类**。
- 数字列用 `font-mono` 并右对齐 —— 等宽是为让小数点对齐后能竖着扫。
- 状态色只用既有 `--color-success-*` / `--color-warning-*` / `--color-danger-*`。
- `--radius-card: 0.125rem` 是硬边，不加圆角。
- 行内钻取用 `AdminModal.vue`：点行 → 弹窗列出该接口最近 N 条慢请求
  （`ts` / `status` / `duration_ms` / `request_id`），`request_id` 可点，跳 `/logs?request_id=…`
  复用既有日志检索。若 `LogsView` 目前不读 `request_id` query，就在 T14.4 顺手补上
  —— 这是现成排障路径的闭合，不是新功能。

---

## 8. 取舍与不做

| 议题 | 取 | 舍 | 记录 |
| --- | --- | --- | --- |
| 数据来源 | 复用 `app_logs` 纯查询 | 新增采集链路 | 本节 3 |
| 流式接口的耗时 | 单独成表、字段名 `setup_*`、不给数字 | 与普通接口同列排行 | 本节 2-1 |
| 错误率 | 拆 4xx / 5xx，只 5xx 染色 | 一个笼统 `error_rate` | 本节 5.1 |
| 索引 | v21 不加，留三级阶梯 | 现在就加表达式索引 | 本节 6 |
| 时间维度 | 只有窗口内聚合 | 时间序列曲线 / 分桶 | 本节 1.3 |
| 历史 | 只读日志窗口 | 另存指标快照 | 本节 1.3 |
| 空态 | 五态分派，其一是成功绿 | 统一灰色空态 | 本节 7.3 |

---

## 9. 任务拆分

| 编号 | 内容 | 涉及文件 | 依赖 | 验收 |
| --- | --- | --- | --- | --- |
| **T14.1** | 契约：Pydantic 模型与路由骨架 | `api/schemas.py`（`EndpointMetricsResponse` / `EndpointRow` / `StreamEndpointRow` / `MetricsCoverage` / `MetricsThresholds`）、`api/admin.py`（`GET /metrics/endpoints`，套 `require_admin`） | — | OpenAPI 可见；非管理员 403；`top` 越界与时间倒置返回 400 |
| **T14.2** | 聚合查询 | `repositories/log_repo.py`（`aggregate_endpoints` / `aggregate_stream_endpoints` / `coverage` / `dropped_count_since`） | T14.1 | 单测：空表返回空；1 条不出分位数；`n=20` 时 p95 取第 19 个（钉住最近秩算法）。**丢弃数三情形各一条**：0 条事件 → `0`、1 条 → `null`、≥2 条 → `MAX-MIN`。并做**反向验证**：把实现临时改成 `SUM`，「≥2 条」那条必须转红 —— 否则守卫是假的 |
| **T14.3** | 编排：SSE 分类、阈值套用、coverage 组装、错误率拆分 | `services/admin_service.py`（`endpoint_metrics()`） | T14.2 | 5xx / 4xx 分别算出；流式行只有 `setup_*` 字段；`insufficient_samples` 里没有任何分位数字段 |
| **T14.4** | 前端页面与导航 | `frontend_admin/src/services/adminService.ts`（`fetchEndpointMetrics`）、`src/views/PerformanceView.vue`（新建）、`src/router.ts`、`src/nav.ts`、`src/views/LogsView.vue`（读 `request_id` query，若尚未支持） | T14.3 | 五种空态各自可复现；流式表里不出现耗时数字；`npm run build` 与 `type-check` 通过 |
| **T14.5** | 测试与守卫 | `frontend_admin/src/__tests__/performance.spec.ts`、`backend/tests/test_admin_metrics_api.py`、`backend/tests/test_sse_path_guard.py`（均新建） | T14.3 | 五种空态各有用例；小样本分位数用例；**SSE 路径守卫**：断言「代码里的 SSE 路径清单」与「实际分流出的流式路径」完全一致，并配自检用例证明守卫没瞎 |
| **T14.6** | （可选）v21 迁移 | `core/migrations.py` + `core/db.py` | T14.2 实测 | **默认不做**。仅当 `elapsed_ms > 1000` 且 `window_request_rows` 接近上限时才按 6.2 第 1 步加部分索引；若加，**两处都要改**（`migrations.py` 与 `db.py` 的 `SCHEMA_SQL`），否则新库老库分叉 |

**为什么 T14.5 必须有「SSE 路径清单守卫」**：`setup_*` 与 `p50_*` 的分流靠「路径是不是流式」
这个判断。它一旦漏判，症状是**流式接口被当成普通接口、带着 40 毫秒混进耗时排行** ——
正是本任务最想避免的那个误读，而且**它不会报错，只会安静地给出错数字**。
所以这条判断要有守卫钉住，沿用仓库既有的 `test_layering.py` 写法（含 `is_not_vacuous` 自检）。

---

## 10. 与 v5 其它部分的关系

- **不改任何产品行为**：只新增一条只读管理端点与一个只读页面。
- **与 `ARCHITECTURE.md` 第 7 节（SSE 传输契约）呼应**：那节讲「SSE 的分帧与响应头本身是契约」，
  本节讲「SSE 的 `duration_ms` 不是生成耗时」。两者合起来才是 SSE 在可观测性上的完整口径。
- **与 ADR-066（测量先行）同一条纪律**：`elapsed_ms` 进响应，就是不让「要不要加索引」靠猜。
- **不引入新依赖**：前端不需要图表库（没有曲线），后端不需要额外包（JSON1 内置）。
