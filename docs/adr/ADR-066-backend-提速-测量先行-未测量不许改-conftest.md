# ADR-066：backend 提速——测量先行，未测量不许改 conftest

> 源：v5 层（原 `docs/architecture/v5/ADR.md`，已并入本目录）｜ [索引](./README.md)

> 状态：**假设已被推翻**（T04 执行完毕，改判见 ADR-071）。正文保留作为「先测量再动手」的记录。

### 背景

backend 全量 863 例耗时 362.7 秒。怀疑主因是 `tests/conftest.py` 的 `isolated_db`
是 autouse 夹具，863 次建库。但此结论是推断，未经 profiler 验证。

### 决策

#### 第一步：改任何东西之前必做的两条命令

```bash
# 实验 A：是「每个用例都慢一点」还是「少数用例极慢」——决定策略方向
uv run pytest -q --durations=30 2>&1 | tail -40

# 实验 B：直接检验假设，init_schema 到底多贵
# （每次用全新文件，避免 CREATE TABLE IF NOT EXISTS 在第二次短路）
uv run python -c "
import time, tempfile, os, pathlib
import backend.core.config as c
from backend.core.db import init_schema
t = time.perf_counter()
for _ in range(20):
    os.environ['DB_PATH'] = str(pathlib.Path(tempfile.mkdtemp()) / 't.db')
    c.get_settings.cache_clear()
    init_schema()
    d = time.perf_counter() - t
print('init_schema x20 = %.2fs  →  862 次约 %.0fs' % (d, d * 43.1))
"
```

判据：

- 若实验 B 的结果与 362 秒同量级 → 假设成立，走方案甲；
- 若只占零头 → 假设被推翻。改用实验 A 的 `--durations` 找真正的热点，
  别在 conftest 上白费力气。

#### 方案甲（假设成立时）：模板库 + 文件复制

session 级夹具只建一次库，之后每个用例把模板 `shutil.copy` 到自己的 tmp 路径。

安全性论证（本方案成立的关键）：

1. `init_schema()` 全是 `CREATE TABLE IF NOT EXISTS`，产出的是一个空库；
2. conftest 本来就不跑迁移（20 步迁移只在 `test_migrations.py` 里跑，
   这一点 `core/migrations.py` 自己的注释写明了）；
3. 因此「复制一份空库」与「现建一份空库」行为完全一致，零行为变更。

两条实现注意：

- 模板必须在连接关闭之后再复制。这里恰是 `cp` 安全的场合（新建即关、无在途写入），
  与既有认知「备份不能 `cp`、要用 `VACUUM INTO`」不冲突——后者针对有在途写入的库。
- 保留每用例一把 `Fernet.generate_key()`：成本约 10 微秒可忽略，
  且它保证密钥用例互不串味。

#### 明确否决的方案乙

把 `isolated_db` 改成 session 级、全套件共用一个库。
这等于直接制造 conftest 文件头自己写着的那个问题（原文：否则用例之间会互相
看见对方注册的用户、核销掉的邀请码，测试结果随执行顺序漂移）。提速再多也不换。

### 验收标准（本条是全层最值钱的一条）

> 改动前后，`pytest --collect-only -q` 的总数必须完全相同（**当前 867**），
> 且 pass / fail 集合逐条一致（当前基线是 **867 全绿**）。
>
> 这两项任一不满足，提速视为失败并回滚。

理由：「快了但少跑了 5 个用例」是这类夹具改动最容易犯、
且只看耗时完全看不出来的错。 只看耗时会让这类改动以「优化成功」的姿态通过验收，
而实际是静默地削弱了套件。

### 代价

多一个 session 级夹具与一份模板文件；
模板本身需要正确关闭连接，否则 WAL 下有踩坑风险（已写入注意事项）。

### 落地清单

1. 跑实验 A、B，记录数据；
2. 若甲成立，改 `backend/tests/conftest.py` 约 15 行；
3. 对拍验收（收集数 + pass/fail 集合）；
4. 记录改动前后耗时，写入 T12 的测试基线。

### 结果（T04 已执行）：假设被推翻

正文全部保留 —— 它是一份「先测量再动手」的记录。但结论必须标出：

- 实验 B 实测 `init_schema` **26.7 ms/次 × 867 = 23.1 秒，只占 8%**
  → 「主因是 867 次建库」**不成立**；方案甲（模板库 + 复制）最多省 22.7 秒。
- 实验 C 实测 `client` 夹具净增 **147 ms/用例**，真热点是「每个用例起停一整套应用」。
- 因此 T04 **没有改 `conftest.py`**，改判为默认并行，见 ADR-071。

**⚠️ 本节方案甲安全性论证的第 2 条是错的，一并更正（别让下一个人再被它误导）：**

原文写「conftest 本来就不跑迁移（20 步迁移只在 `test_migrations.py` 里跑）」。
**不成立**：`isolated_db` 只调 `init_schema`，但 `main.py` 的 lifespan 每次都调
`init_schema()` + `run_migrations()`（`main.py:61`），所以**每个用 `client` 的用例
都真的把 20 步迁移铺了一遍**（25.2 ms）。

于是方案甲根本没有它声称的「零行为变更」—— 它省得掉建库，**省不掉迁移**。

这条错误本身值得记一笔：**读某个模块的 docstring 不足以确认运行期行为**
（`core/migrations.py` 的注释说的是「conftest 只调 init_schema」，字面没错），
必须看**真实启动路径** —— 而 `client` 夹具走的正是 `main.py` 的 lifespan。

### 验收标准的两处修正（T04 实际执行时发现）

1. **原写「pass/fail 集合逐条一致」，但基线本身不稳定。** 同一份代码两次全量分别得到
   `1 failed, 862 passed` 与 `863 passed`；失败点是
   `test_admin_logs.py::test_pagination_reports_total_not_page_size`
   （单独跑通过、整文件单独跑 10 passed），另有 `test_auth_service.py` 的 READONLY
   线程异常被吞成 warning。
   → 对拍必须先有**已知 flaky 白名单**，且约定「**白名单不得新增，新增即视为回归**」。
2. **基线数字必须来自同一次干净采集。** 本次出现过 362.7s 与 335.7s 两个「基线」，
   差距来自采集时间点不同。拿两批数字互相比较会得出错误结论。

---
