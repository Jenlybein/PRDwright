# ADR-081

> 源：v6 层（原 `docs/architecture/v6/ADR.md`，已并入本目录）｜ [索引](./README.md)

### 连接池交给 SQLAlchemy，守住 `max_overflow=0`

**状态**：已定

**背景**

T15 的自建池解决的是一个真实问题：`open()` 时一次性建满连接，让「多线程同时首次映射
WAL `-shm`」没有发生的时机。换 SQLAlchemy 后必须保留这条**结构性**不变量，
而不是退化成「需要时再建」。

**决定**

```python
create_engine(..., pool_size=N, max_overflow=0, pool_timeout=5.0)
# 启动期显式建满：
held = [engine.raw_connection() for _ in range(N)]
```

- `max_overflow=0`：不允许运行期扩容。池满 → `TimeoutError`，而不是「悄悄建一条新连接」；
- `pool_timeout=5.0`：与今天的 `acquire_timeout` 一致，避免请求无限挂起；
- 启动期 `warmup()` 显式建满：保证「运行期不建连接」这条不变量成立。

**理由**

`max_overflow>0` 会让「并发峰值时建连接」，而建连接正是那个 WAL 竞态的触发条件。
池满时宁可报错（可观测、可调 `DB_POOL_SIZE`），也不要悄悄扩容把竞态请回来。

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| `NullPool`（每次新建） | 直接回到 T15 要修的竞态，且每次付建连成本 |
| `StaticPool`（单连接共享） | 并发请求会串行化，且 `check_same_thread` 语义混乱 |
| 默认 `QueuePool`（`max_overflow=10`） | 高并发时扩容建连接 → 竞态窗口重现，且症状是偶发 `SQLITE_READONLY`，极难复现 |

**后果**

- `DB_POOL_SIZE` 仍是唯一调参口；`stats` 里的 `wait_ms_max` 改成从 SQLAlchemy 的
  pool 事件采集（或保留一层薄封装）；
- T15 的两个 P0（漏还连接、dispose 后复活）由 SQLAlchemy 的 `QueuePool` 承担；
- `test_database_pool.py` 里 8 条池专属用例删除，保留的 2 条语义改写为「池满报错」与
  「dispose 后借用报错」。

---
