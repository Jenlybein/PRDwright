# ADR-084：连接池预热进启动路径（`warmup()` 必须在开始服务之前跑）

> 源：v7 层（原 `docs/architecture/v7/ADR.md`，已并入本目录）｜ [索引](./README.md)

**状态**：已定（v7 新增，**T17.1 已落地**）。

### 背景

`core/engine.py` 的设计不变量是：**连接只在启动期建满，运行期一条都不新建**。
它的落地方式是 `QueuePool(pool_size=4, max_overflow=0)`——池满即等 `pool_timeout`（5 秒），
然后抛超时，**不会悄悄扩容**。

这条不变量不是洁癖。T15 时期踩过一个具体的坑：多线程**同时首次映射** WAL 的 `-shm` 文件
会返回 `SQLITE_READONLY`，而 `busy_timeout` 只对 `SQLITE_BUSY` 生效，所以它不重试、
直接失败。当时的解法就是「把建连接这个动作收敛到启动期单线程」。

v6 把自建的 444 行池换成了 SQLAlchemy，但**保留了同一条不变量**，并提供了
`engine.warmup()` 来实现它。

### 现状（这是本 ADR 存在的全部理由）

```
$ grep -rn "warmup" --include=*.py src tests
src/backend/core/engine.py:120:def warmup(...)
tests/test_engine.py:53:def test_warmup_builds_all_connections_before_serving(...)
tests/test_engine.py:67:    assert engine_module.warmup(eng, 3) == 3
```

**只有测试在调它。** `main.py::lifespan` 里没有。

于是「连接只在启动期建满」这条不变量在生产上不成立：池是懒建的，实际只会建到
「真被同时用到」的那几条。`prepare_schema()` 借过又归还 1 条，所以启动那一刻池里是 1 条——
`DB_POOL_SIZE=4` 成了一个没有兑现的配置。

### 谁会同时碰数据库（窗口到底在哪）

这一点必须先看清，否则会修错地方。本项目里能借到池的只有两个角色：

| 借用者 | 并发性 | 说明 |
| --- | --- | --- |
| HTTP 请求处理 | **串行** | 所有路由都是 `async def`，内部直接调同步的 service；同步 DB 调用会阻塞事件循环，所以同一时刻只有一个请求在碰库 |
| `log-store` 后台线程 | **与上者并发** | `core/logging_setup.py` 里 `threading.Thread(name="log-store")` 从队列取日志批量写 `app_logs`，走同一个池 |

两者在**启动期**天然重叠：`install_log_persistence()` 起线程之后，主线程还要继续跑
`ensure_bootstrap()` / `reap_orphans()` / `ensure_skills()`，每一步都可能借连接。
第一处真正「两个线程同时建连接」的时机就在这里。

**不在「第一批用户请求」**：那时启动已经完成，日志线程也早把连接借出来了，
池里那几条已经建好。这一点与 v5 起草时的判断不同（当时写的是「启动完成后第一批并发请求」），
按当前代码核过之后修正。

### 决策

在 `lifespan` 里、`prepare_schema()` 之后、任何可能借到池的后台设施之前，显式预热：

```python
from backend.core.engine import get_engine, warmup

revision = prepare_schema(get_engine())
warmup(get_engine())          # ★ 建满 DB_POOL_SIZE 条，只在此处建连接
invalidate()
install_log_persistence()     # 日志线程之后才可能借连接
```

位置三条理由：

- **在 `prepare_schema` 之后**：迁移期间借的那条连接先归还，预热才是「从头建满」；
- **在 `install_log_persistence` 之前**：否则日志后台线程可能与预热并发建连接，
  把竞态请回来（虽然窗口很小，但这条不变量的价值就在于「窗口为零」）；
- **在接请求之前**：这是显而易见的，但它决定了「预热失败 = 启动失败」，
  而不是「第一个用户遇到 500」。

### 被否决的方案

| 方案 | 否决理由 |
| --- | --- |
| 调大 `pool_size`（比如 16）不预热 | 没用。懒建的池无论多大，都是「第一批并发请求里同时建」，竞态窗口与原池相同 |
| 允许 `max_overflow > 0` | 把 T15 的竞态请回来。溢出连接正是「运行期新建连接」 |
| 把 `pool_pre_ping=True` 当成解药 | `pre_ping` 解决的是「连接被服务端断开」，不是「同时在建连接」。两件事 |
| 启动后人工打几个并发请求「预热」 | 不可重复、不可验证，且发布脚本里没人会记得 |

### 后果

- **启动多一次固定开销**：建 4 条 SQLite 连接，毫秒级。相比「第一批用户撞竞态」的代价可忽略。
- **预热失败即启动失败**（例如目录只读、磁盘满）。这是好事：它把「库根本连不上」
  从「运行期偶发 500」提前成「进程起不来」，而后者在 `systemctl status` 里一眼可见。
- **要用例守住**（已加，全部在 `tests/test_engine.py`）：
  1. 串行借还 5 次，新建连接数不增（原有）；
  2. **并发**借 8 次，新建连接数不增（新增，用线程池造出「同时第一次借」那个瞬间）；
  3. **`lifespan` 真的调用了它**（新增）。
  第 3 条最关键：前两条只测 `warmup()` 这个函数本身，`main.py` 里漏掉那一行时它们照样绿——
  那正是 v7 之前的状态。
- ⚠️ **判据只能是 `checkedin()`，不能是 `size()`。** `QueuePool.size()` 返回的是
  `maxsize`（配置容量），恒等于 `DB_POOL_SIZE`，与实际建了几条无关。
  第一版用例拿 `size()` 断言，反向验证时发现「完全不预热」也能通过。
  顺着这条线还查出两处假象：`engine_stats().idle` 原来算的是 `maxsize - in_use`
  （同样恒真），已改成 `checkedin()`；`conftest` 收尾守卫里那条 `idle == size`
  因此**删掉**——它从来没守过任何东西。
- **实测**：不预热时 `{'size': 4, 'in_use': 0, 'idle': 1}`，池里真的只有 1 条；
  预热后 `idle` 等于 `DB_POOL_SIZE`。这就是「配置写 4、实际 1」的量化证据。
- **测试开销**：每个走 `client` 夹具的用例都会因 `lifespan` 多建 4 条连接。
  v6 的装置是「每进程模板库 + 每用例拷贝」，做不到真正的进程级预热（每用例换库）。
  实测全量 **986 例 / 92.9 秒**，与修前（984 例 / 95.3 秒）在并行波动范围内——
  预热开销可忽略。若将来用例数翻倍再考虑把测试里的 `DB_POOL_SIZE` 调小。

---
