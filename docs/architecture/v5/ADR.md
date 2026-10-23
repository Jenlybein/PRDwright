# v5 决策记录

> 编号接续 `../v4/ADR.md`（最大为 ADR-053），从 ADR-060 起。
> ADR-054~059 是原「v5 部署与上线」的六个编号，按 ADR-070 保留为预留墓碑。
>
> 每条记录回答四件事：背景 / 决策 / 代价 / 落地清单。

---

## ADR-060：测试分层用 marker 表达，不做目录重组

> 状态：已评审，待落地（T09）

### 背景

54 个测试文件平铺在 `backend/tests/`。QA 诊断给出的分层现状是
「unit / service 334 例（38.7%）、接口层 529 例（61.3%）」，但那 61.3% 是虚高的：
`test_generation_job.py`、`test_clarify.py`、`test_auto_clarify.py` 等核心文件
主要直接调 `job_service.create_job` / `dispatch` / `open_stream`，
并大量以 `# noqa: SLF001` 直触 `_RUNTIMES` / `_TASKS` / `_finalize` 等私有成员，
`client` 只用于造数据。真正做 HTTP 契约断言的集中在 admin 组、auth、documents、
solutions、export。

直觉解法是建 `unit/` `service/` `api/` 三个子目录，把 54 个文件搬进去。

### 决策

不搬目录。 54 个文件原地不动，每个文件顶部加一行 `pytestmark = pytest.mark.<layer>`，
四个 marker（`unit` / `service` / `api` / `integration`）在 `pyproject.toml` 注册。

配套一条静态守卫，断言「没有 marker 的测试文件数为 0」。

### 为什么

- 搬目录只换容器不换内容，收益为零；代价是 54 个文件的 git 历史被打散
  （`git log --follow` 失效），且下次想再调整又是一次大 churn。
- marker 能做到目录做不到的两件事：`pytest -m api` 只跑 HTTP 契约、
  `pytest -m "not integration"` 快速循环。
- 可行性有既有依据：本仓库已有 `backend/tests/test_layering.py`，
  用静态扫描守「services / repositories 不得反向 import api」，
  并且它配了两条 `..._is_not_vacuous` 自检用例。这是本项目既有的 idiom。

### 代价

分层因此是约定而非物理约束：有人不加 marker 也能混过去。
缓解手段就是那条守卫——它同时还能防「新建测试文件时忘标」。

### 落地清单

- `backend/pyproject.toml` 注册四个 marker。
- 54 个 `backend/tests/test_*.py` 各加 1 行 `pytestmark`。
- 守卫 1 条（含自检用例）。

---

## ADR-061：集成测试只覆盖「传输层即契约」的接口，止于 SSE 一族

> 状态：**已推迟**（用户决定集成测试与系统测试本期都不做，之后再做）。
> 本条的判定规则保留，作为将来重启这项工作的依据；
> 本期的处理见文末「推迟后的替代动作」。

### 背景

integration 为 0，e2e 为 0。但本项目是单进程 + 单 SQLite + 单 app，
没有多服务、没有消息队列、没有跨进程边界。

### 决策

判定规则：只有当「经过 HTTP 层」本身会改变可观测行为时，才写集成测试。

- 对普通 REST 接口，HTTP 只是搬运工，测 service 就等于测了集成 → 不写；
- 对 SSE，分帧节奏、响应头、连接语义本身就是契约，绕开 HTTP 就等于没测 → 必须写。

按此规则扫描，命中的只有 SSE 一族：`/api/jobs/{id}/stream` 与 `conversation/*-stream`。
加满这一族就停。

明确不做反面：不给每个接口补一条 HTTP 冒烟。

### 为什么

这个仓库里不存在天然的集成第二层，硬造一个造出来的就是 HTTP 冒烟
（40 多个用例，覆盖的是 TestClient 自己）。

### 代价

`X-Accel-Buffering` 这类头在 TestClient 下即使断言了，也不能证明 nginx 真的不缓冲
——仓库里没有 nginx 配置可对。

> ⚠️ 因此本 ADR 附带一条边界声明：SSE 集成测试守的是「后端发出的头是对的」，
> 不是「生产链路是通的」。 后者要等 v6 容器化之后才有靶子。
> 同理，真实断线重连在 jsdom 下不做自动化覆盖（jsdom 无真实网络中断）。

### 落地清单（推迟后的替代动作）

本期**不建** `backend/tests/integration/` 目录，也不新增集成层工程，整条 T05 推迟到之后。

但保留一个最小例外，理由是不留它就有个「生产必炸、本地必绿」的洞：

- 在**现有的** HTTP 接口测试里补 3 条响应头断言
  （`Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`），
  复用 `test_chat_http_flow.py` 里已有的 `client.stream(...)` 写法；
- 不新建文件、不建目录 —— 就是那个文件里多 3 行 `assert`。

这**不构成**集成测试：它仍在 TestClient 同进程内，验的是「后端发出的头对不对」，
不是「Nginx 有没有真的关掉缓冲」（后者要等容器化有靶子才验得了）。

原先计划的第 4、5 条（`Connection: keep-alive`、跨 chunk 半帧拼装 + `[DONE]` 哨兵）
一并推迟 —— 它们需要构造跨 chunk 的流，属于真正的传输层测试。

---

## ADR-062：v5 不建 e2e，触发条件命中才重新评估

> 状态：**已确认推迟**（用户明确「系统测试之后再做」，与本条一致；本条是范围决策，无代码落地项）

### 背景

两个 `vitest.config.ts` 的 `exclude` 里都已经含 `'e2e/**'`，
但 `e2e/` 目录根本不存在——门留好了，房间里没人。

> 这个细节要写准：那条 `exclude` 是照 Vite / Vitest 默认模板抄来的，
> **不是本项目有意为之**。所以 e2e 的正确表述是「**从未被认真考虑过**」，
> 而不是「我们评估后决定不做」——后者会给人一种已经权衡过的错觉。
> 本 ADR 是这一层第一次把 e2e 当作正式议题记录。

### 决策

不建 e2e。四个触发条件命中任一即重新评估：

| # | 触发条件 | 当前状态 |
| --- | --- | --- |
| E1 | 仓库出现 `Dockerfile` / compose / `nginx.conf`（有了可复现的生产环境） | 未成立 |
| E2 | 出现第二个前端（移动端 / 小程序）共用同一后端 | 未成立 |
| E3 | 引入真实注册 / 支付等不可 mock 的外部依赖 | 未成立 |
| E4 | 出现第二个维护者（selector 维护成本可被分摊） | 未成立（单人） |

### 为什么不是无限期拖延

v5 之后紧接着就是 v6 部署，而容器化是部署的自然产物，所以 E1 在 v6 就会被触发。
e2e 的落点是「v6 后期重评」，不是「永远不做」。这个判断有终点。

### 代价

跨端自动化回归直到那时都靠人工。

### 落地清单

- 本 ADR 本身。
- 在 `roadmap.md` 的 T13 挂一条「v6 期重评 e2e」的待办。

---

## ADR-063：性能与安全只做护栏，不做体系

> 状态：已评审，待落地（T05、T06）

### 背景

- 导出 50MB 上限（`export_service.MAX_EXPORT_BYTES`）后端零测试，前端只断言了提示文案；
- `test_auth_api.py` 只断言 `Secure not in set_cookie`，缺少正向断言；
- 无静态安全扫描器。

### 决策

性能：2 条上限回归守卫（导出 50MB、生成并发写锁）+ 1 条 CI 级测试耗时护栏（ADR-069）。
不做性能测试（不测「生成一篇 PRD 要多久」），不引 k6 / locust / benchmark。

安全：5 条定向用例（见下）。不引 bandit / safety。

### 为什么

- 性能：测产品性能没有靶子（无目标机配置），测测试性能有靶子（362.7 秒每天都在发生）。
- 安全：业务代码不写裸 SQL（走 `repositories/`，`test_layering.py` 仍在守），
  bandit 在 FastAPI + sqlite3 项目里对每个 `execute()` 都会报，噪音远大于信号。
  每天多一批必须「确认无害」的告警，很快会被整体忽略，连带真告警一起丢。

### 代价

无扫描器 = 依赖作者自己想到要测什么。
缓解：把 5 条用例挂在本 ADR 里，作为 T06 的验收清单，可逐条核对。

### 落地清单

| # | 用例 | 落点 |
| --- | --- | --- |
| 1 | `auth_cookie_secure=true` 正向断言 | `backend/tests/test_auth_api.py` |
| 2 | `reveal-key` 越权（非管理员访问） | `backend/tests/test_admin_config_api.py` |
| 3 | 导出超 50MB → 413 | `backend/tests/test_export.py` |
| 4 | 生成并发写锁上限 | 后端并发用例 |
| 5 | SSE 响应头（与 ADR-061 合并） | `test_sse_transport.py` |

> 另有一条性质特殊的：`test_auth_service.py` 的邀请码并发原子性用例目前
> 吞掉线程异常，对「并发下邀请码超发」是失明的。修它属于安全，不属于清理，
> 因此记在 T06 而不是 T07。

---

## ADR-064：用棘轮守卫取代存量大扫除

> 状态：已评审，待落地（T07、T08）

### 背景

863 个存量用例里有三类坏味道：`noqa: SLF001` 私有成员直触、
跨文件重名用例、3 个因重名被覆盖而永不执行的死用例。

### 决策

不做 863 例批量改造。 改为四条静态守卫，其中三条用棘轮
（记录基线常数，只许降不许升）：

| 守卫 | 断言 | 能抓住什么 |
| --- | --- | --- |
| G1 收集数 = def 数 | 全仓 `def test_*` 总数 == pytest 收集总数 | 自动抓住 `test_login_lockout.py` 那 3 个死用例（18 defs 只收集 15） |
| G2 SLF001 棘轮 | `noqa: SLF001` 出现次数 ≤ 基线常数 B | 私有成员直触只许减少不许增加 |
| G3 重名用例 | 无两个文件定义同名 `test_xxx` | 抓住 `test_migrations_reach_latest_version` 这类跨文件重复 |
| G4 时长护栏 | 单文件耗时 ≤ 阈值 | 慢测试劣化可见 |

### 为什么

棘轮模式不需要一次性清理 863 个用例，但保证明天不会比今天更差；
每做一次清理就把基线往下拧一格，拧下去就回不来。

G1 尤其值得：它是一个能自动发现死用例的守卫。
`test_login_lockout.py` 那 3 个永不执行的用例是靠人工重名分析发现的，
装上 G1 以后这类问题会自己浮出来。

### 代价

存量坏味道会保留一段时间——这是有意的。
它们已被守卫登记在案，且每修一处就永久拧低一格。

### 落地清单

- 新增 `backend/tests/test_test_quality_guards.py`：4 条守卫 + 4 条自检用例。
- 自检用例沿用 `test_layering.py` 的 `..._is_not_vacuous` 惯例
  （用已知坏代码证明守卫没瞎）。
- G2 的基线常数 B 必须在 T07 清理之后按实际值记录，不能用清理前的数字。

---

## ADR-065：前端共享状态先抽 support 模块，再谈拆文件

> 状态：已评审。本条与 QA 初稿结论相反，保留本论证

### 背景

`AppV2.spec.ts` 2,991 行 / 83 个 `it`，占 frontend 测试行 20.6%，且是当前最红的文件
（15 个失败）。其中确实存在一处共享状态泄漏：`turnHarness.items` 从不重置，
而 `jobHarness` 有重置——两个 harness 的重置不对称
（`jobHarness` 专写了 `reset()` 方法，说明作者清楚跨用例泄漏，只是漏了一个）。

QA 初稿结论是「要拆先拆 `AppV2.spec.ts`」。

> ⚠️ 本 ADR 初稿曾把 `turnHarness` 当成这 15 个失败的根因，该判断已被实测推翻。
> QA 逐个 `-t` 单独跑的结果：补上这一行只让 1 个转绿，14 个在完全隔离下照样红。
> 且 6 个文件单独跑与全量跑的失败数完全相同，32 / 33 与顺序依赖无关。
>
> 因此本 ADR 的适用范围收缩为：这一行该修（止血 + 消除潜伏隐患），
> 但它不解释那 15 连红。
> 那 32 个的处置见 T02a：**18 条已有机制可直接改，16 条待定按「先改测试 → 仍红再查源码」分流**。

### 决策

v5 不按 describe 拆文件。 顺序为：

| 顺序 | 动作 | 时期 |
| --- | --- | --- |
| ① | 按三条已证机制改测试（18 条，含本 ADR 那一行；机制见 `ARCHITECTURE.md` 第 6.2 节） | T02 |
| ② | 16 条待定按「先改测试 → 仍红再查源码」分流 | T02a / T02 |
| ③ | 抽 `support/appV2Harness.ts` | T11 |
| ④ | 按 describe 拆文件（P3，可不做） | v5.x 之后 |

### 为什么（反对立即拆分的三条理由）

1. 它不是真的臃肿，它已经分好层了。 2,991 行里有 8–9 个现成的 describe 块。
   内部结构清晰，不好的是共享状态的管理，不是文件的划分。
2. 按 describe 拆成 8 个文件，会把第 34–400 行那 380 行 mock 装置复制 8 份。
   以后改一次装置要改 8 个文件。这是把「一个大文件」换成「八处重复」，净损失。
   QA 已认可这条理由并撤回其初稿建议。
3. 那 380 行装置被 83 个用例共享，**任何一次改动的爆炸半径是 83**。

> 关于第 ③ 步的目的，要把两条理由都写进去（QA 撤回其初稿建议后补充的，
> 与我的理由互补，缺任一条都不完整）：
>
> - **我的理由**：按 describe 拆 8 份会把那 380 行装置复制 8 份，
>   是「一个大文件」换成「八处重复」，净损失。
> - **QA 的理由**：抽 `support/appV2Harness.ts` 的目的**不是为了拆文件**，
>   而是那 380 行被 83 个用例共享，**改一行的爆炸半径是 83**——
>   抽出去是为了**收敛爆炸半径**。
>
> 两条合起来才说明白：不拆是为了不制造重复，抽是为了收敛改动面。

> ⚠️ 第 ③ 步的收益要写准，别让人误读：抽成模块只解决跨文件，
> 同一文件内 83 个 `it` 之间仍然共享装置实例，
> 所以 `reset()` 仍然必须显式写在 `beforeEach` 里。抽完不等于自动免疫。
> （这一点是 QA 提醒后补上的，初稿表述过度乐观。）

### 代价

放弃了 Vitest 文件级并行（8 文件 = 8 并行单元）能带来的 frontend 提速。
这是第 ④ 步唯一额外的好处——想要并行时才做，否则不做。
它对解决当前这 34 个失败没有帮助（那些失败既非状态泄漏，也非文件太大）。

### 落地清单

- T02a：16 条待定的施工期分流（18 条已在 triage 阶段定性完毕，带源码位置与引入提交）。
- T02：第 382 行 `beforeEach` 补 `turnHarness.items.length = 0`（只解决 1 个，止血性质）。
- T11：新增 `frontend/src/__tests__/support/appV2Harness.ts`
  （导出两个 harness + 全部 `vi.mock` + `installDefaults()`），
  复用既有 `support/solutionState.ts` 的先例。
- T11 验收含反向验证：临时破坏 harness 的重置 → 必须转红 → 还原。

---

## ADR-066：backend 提速——测量先行，未测量不许改 conftest

> 状态：已评审，待落地（T04）

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

---

## ADR-067：三工程开 coverage，只看不设阈值

> 状态：已评审，待落地（T10）

### 背景

两个 `vitest.config.ts` 都只配了 `environment: 'jsdom'`，无 coverage；
后端未配 `--cov`。因此 0.752 / 0.776 两个密度数字不说明任何覆盖率。

### 决策

三个工程都能出覆盖率数字。v5 不设任何百分比门槛。

### 为什么

设阈值会立刻诱发「为达标而写的测试」（给 getter 写断言），是典型的指标扭曲。
v5 的目标是看见，不是达标。 门槛留到看到真实数字之后再定。

### 代价

每次跑全量多几秒。

### 落地清单

- `frontend/vitest.config.ts`、`frontend_admin/vitest.config.ts` 加 coverage 配置。
- `backend/pyproject.toml` 配 `--cov`。
- CI 三工程各自产出报告（不强校验）。

---

## ADR-068：CI 补齐 admin job

> 状态：已评审，待落地（T01）

### 背景

`.github/workflows/ci.yml` 只有 `backend` 与 `frontend` 两个 job。
`frontend_admin` 的 12 个 spec / 114 个用例本地全绿，但从未在 CI 上跑过。

### 决策

加 `frontend-admin` job，三步：`npm ci` → `npm run test:unit` → `npm run build`。
**同时给 admin 加 `type-check`**——目前 admin 在 CI 里连类型检查都没有。

**并一并立起「CI 红即阻断」的纪律。**

> 这条比补任何一条测试都重要。已实测的事实：**CI #4 的 frontend job 已经报了
> 10 个 AppV2 错误，`origin/main`（`3aea2c2`）本身就是红的。**
> 主分支红着却没人修，说明**补再多测试也会被无视**——因为套件已经失去了否决权
> （这正是 Q1 那个核心问题的根因）。
>
> 所以 T01 不只是「加一个 job」，它修的是**让测试重新拥有否决权**这件事。

### 为什么

- 已核对可用：`frontend_admin/package.json` 的 `test:unit` 是 `vitest run`
  （已自带 run，不像 frontend 那样是 watch 模式），CI 可直接调，无需传 `--run`。
- 114 个用例是已经写好的资产，现在的状态是资产闲置。

### 代价

admin 进 CI 后，admin 的红灯会开始挡 push——这正是目的。

### 落地清单

- `.github/workflows/ci.yml` 加约 15 行（比初估的 10 行多 5 行，多在这条 type-check）。

---

## ADR-069：把测试自身耗时做成 CI 护栏

> 状态：已评审，待落地（T01）

### 背景

362.7 秒是本项目当前唯一真实存在、且每天都在发生的性能问题。

### 决策

CI 里给 backend job 加硬上限（如 `timeout-minutes: 8`），
并在 ADR-066 完成后按实测值下调。

### 为什么

测产品性能没有靶子，测测试性能有靶子。
这是 Q4「性能」在 v5 期唯一能真正落地的形态——测的不是产品性能，是测试性能。

### 代价

阈值定得太紧会造成偶发红灯（CI 机器波动）。
缓解：先按宽松值起，ADR-066 落地后再收紧。

### 落地清单

- `.github/workflows/ci.yml` 的 backend job 加 `timeout-minutes`。
- T12 记录实测基线，作为后续调整的依据。

---

## ADR-070：原 v5 部署文档——已迁出本仓，ADR-054~059 保留为墓碑

> 状态：**已关闭**。用户已把这批部署文档迁移到仓库之外，本仓不再追踪它的重建。

### 背景

v5 阶段主题由「部署与上线」改为「测试重构」。按 `../evolution-truth.md`
「只认 git 提交，不认记忆与文档自述」的口径核对，原部署文档的实际状态是：

| 检查项 | 结果 |
| --- | --- |
| `docs/architecture/v5/` 在仓库里 | 不存在 |
| `git log --oneline --all -- 'docs/architecture/v5/*'` | 0 条命中，从未提交过 |
| `git stash list` | 空 |
| `git status` | 干净 |

即：那四份文档（`README` / `ARCHITECTURE`（644 行）/ `ADR`（ADR-054~059）/ `roadmap`）
曾产出，但从未落盘。当前唯一副本是 `.workbuddy/memory/2026-10-22.md`
与 `MEMORY.md` 的「v5 部署与上线」节。

> 这也是 `evolution-truth.md` 存在的理由：文档自述与 git 事实又一次不一致了。

### 决策

三件事：

#### 1. 先认账，不当成「已规划未实施」

「已规划未实施」的前提是文档还在。它不在了。
正确表述是：v5 部署方案曾产出，但从未落盘。

#### 2. ADR-054~059 保留为预留墓碑，本层 ADR 从 060 起

依据是本项目既有的惯例：ADR 编号是身份，不是时序。

- ADR-042 被 ADR-053 取代后，042 的编号不动；
- ADR-027 被 ADR-028 推翻后，027 的编号不动。

保留 054~059 空号，等于在编号轴上留下一条记录：这里曾有六条部署决策。
将来 v6 部署期按原号重建，零重编号；两个内存文件里对 054~059 的引用
也全部继续有效。

#### 3. 素材已迁出本仓，本仓不再追踪重建

用户已把这批部署文档迁移到仓库之外。因此本仓不保留副本，也**不安排**任何重建任务。
下表只为说明 054~059 这六个空号曾是什么，不构成待办：

| 编号 | 决策摘要 |
| --- | --- |
| ADR-054 | 部署形态 = 单域名 + 路径前缀（用户端 `/`、管理端 `/admin/`、API `/api/`）。否决子域的硬理由：后端 `set_cookie` 没设 `domain`，Cookie 是 host-only，跨子域会「后台登录了、调 API 仍 401」 |
| ADR-055 | 单 worker 是硬约束（推翻旧文档的 `gunicorn -w 2`）：`_RUNTIMES` / `JobBus` / runtime_config 5s 缓存 / SQLite 连接全在进程内 |
| ADR-056 | 四目录分离：`/opt`（代码）/ `/var/lib`（DB）/ `/var/www`（静态）/ `/var/log`（日志）+ `/etc`（backend.env 与 master.key，600） |
| ADR-057 | 回滚 = 恢复备份，不是反向迁移（迁移纪律是只追加） |
| ADR-058 | 备份用 `VACUUM INTO` / `.backup`，不能用 `cp`（开了 WAL）；主密钥随备份同批且保留每一把 |
| ADR-059 | SSE 保活双保险：nginx 的 `proxy_buffering off` + `read_timeout 1800s` + `gzip off`（零代码，必做）；服务端 `: ping` 心跳可选 |

另有几条当时查代码查出来的部署硬事实，一并保留在内存文件里：
`requires-python >= 3.12`（Ubuntu 22.04 自带 3.10 不够）、
Node `^22.18 || >=24.12`（Vite 8）、无 `requirements.txt`（走 `uv sync --frozen`）、
`PRDWRIGHT_MASTER_KEY_PATH` 走 `os.environ` 因此写 `.env` 读不到（必须 systemd `EnvironmentFile`）、
`frontend_admin` 挂 `/admin/` 要改两处（Vite `base` + `createWebHistory(BASE_URL)`，
因为 Vue Router 的 `normalizeBase()` 兜底 `/` 且不跟随 Vite 的 base）。

### 代价

- 编号与期次顺序倒挂：v6 的事编号小于 v5 的事。
  这个代价值得付，因为重编号要动的引用面（MEMORY.md、内存日记、未来的 v6 文档）
  远大于倒挂的别扭。
- 素材已迁出本仓，不在版本控制内；将来要引用需到仓库外查找。

### 为什么不是另外两种处置

| 方案 | 否决理由 |
| --- | --- |
| 原地改写 | 会把 v5 目录变成「测试重构 + 部署」的混合物，本期范围立刻说不清 |
| 降级到 v6 了事 | 等于默认素材还在。它不在——现在不归档，v6 就得从零重新查代码推导 |

### 落地清单

- ADR-054~059 六个空号保持预留，不重编号（本条已完成）。
- 部署文档的归属移交仓库外，本仓不安排重建任务。
- `roadmap.md` 的 T13 不再包含「部署素材重建」这一项。

---

## 编号总表

| 编号 | 标题 | 状态 |
| --- | --- | --- |
| ADR-054~059 | 原 v5 部署与上线六条（预留墓碑，文档已迁出本仓） | 见 ADR-070 |
| ADR-060 | 测试分层用 marker 表达，不做目录重组 | 待落地 T09 |
| ADR-061 | 集成测试只覆盖「传输层即契约」的接口，止于 SSE 一族 | **已推迟**（只保留 3 条响应头断言） |
| ADR-062 | v5 不建 e2e，触发条件命中才重新评估 | 范围决策（已确认推迟） |
| ADR-063 | 性能与安全只做护栏，不做体系 | 待落地 T06 |
| ADR-064 | 用棘轮守卫取代存量大扫除 | 待落地 T07 / T08 |
| ADR-065 | 前端共享状态先抽 support 模块，再谈拆文件 | 待落地 T02 / T11 |
| ADR-066 | backend 提速——测量先行，未测量不许改 conftest | 待落地 T04 |
| ADR-067 | 三工程开 coverage，只看不设阈值 | 待落地 T10 |
| ADR-068 | CI 补齐 admin job | 待落地 T01 |
| ADR-069 | 把测试自身耗时做成 CI 护栏 | 待落地 T01 |
| ADR-070 | 原 v5 部署文档——编号保留，素材重建列为 v6 期待办 | 已评审，重建未执行 |
