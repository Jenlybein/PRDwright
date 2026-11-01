# v5 架构：测试重构

> 本文正向描述 v5 要建成什么样（目标 → 范围 → 四个判断 → 分层 → 提速 → 共享状态
> → 契约 → 取舍），不做「还差什么」的盘点。与 `../v4/ARCHITECTURE.md` 是同一套写法。

---

## 1. 目标与范围

### 1.1 四句话目标

1. 让套件转绿：main 分支上那 34 个失败（前端 33 + 后端 1）归零，并且此后一直绿。
2. 让套件跑得动：backend 全量从 362.7 秒压到 150 秒以内，把「随手就跑」变回现实。
3. 让每条断言都算数：3 个永不执行的用例、1 个吞掉线程异常的用例，全部消失。
4. 让覆盖看得见：三个工程都能出覆盖率数字，而不是只有「测试写了多少行」这种密度指标。

### 1.2 本期范围（In）

| # | 事项 | 依据 |
| --- | --- | --- |
| I1 | `frontend_admin` 进 CI（114 例从 0 变 114） | CI 当前只有 backend / frontend 两个 job，admin 从未被跑过 |
| I2 | 前端 33 个失败 + 后端 1 个失败 → 0 | 红着的套件对新失败没有鉴别力 |
| I3 | backend 362.7s → ≤150s（先测量再动） | 6 分钟反馈循环等于没人本地跑全量 |
| I4 | 新增 SSE 传输契约测试（1 文件 ≤150 行） | 唯一「生产会炸、本地全绿」的缺口 |
| I5 | 安全与护栏 5 条定向用例 | 反向断言缺口 + 导出 50MB 上限零覆盖 |
| I6 | 清理死代码（3 例）与不可靠用例（吞线程异常） | 3 个用例永不执行；1 个用例异常被吞仍判通过 |
| I7 | 测试分层 marker + 棘轮守卫 G1–G4 | 防回退，成本约 30 行 |
| I8 | 三工程开 coverage 采集（只看，不设阈值） | 0.752 / 0.776 两个密度数字不说明任何覆盖率 |
| I9 | 建立测试基线与本层文档，并把 v6 的两条待办挂号 | 让 v5 的收益可交接 |

### 1.3 本期明确不做（Out）

| # | 不做 | 理由 |
| --- | --- | --- |
| O1 | e2e / playwright / cypress | 仓库里 `Dockerfile`、`nginx.conf`、compose 全部不存在，e2e 要模拟的生产环境还不存在；真实 LLM 调用烧 token；单人维护 selector 无分摊 |
| O2 | 性能压测体系（k6 / locust / benchmark） | 真正的性能约束是单 worker（进程内 `_RUNTIMES` / `JobBus` / runtime_config 缓存），压测会先把这个前提打穿，测出来的数字不适用 |
| O3 | 安全扫描器（bandit / safety） | 业务代码不写裸 SQL（走 `repositories/`，`test_layering.py` 还在守）。bandit 在 FastAPI + sqlite3 项目里对每个 `execute()` 都会报，噪音远大于信号 |
| O4 | 54 个后端测试文件按目录重组 | 只换容器不换内容，且会打散 54 个文件的 git 历史。改用 marker（ADR-060） |
| O5 | 863 个存量用例的分层重分类 / 批量摘 `noqa: SLF001` | 863 例级别的手工活，收益是「分层更纯」这种不可观测的东西。改用棘轮守卫锁住不恶化 |
| O6 | 拆 `test_generation_job.py` | 1,528 行 / 59 defs 仅占后端测试行 8.1%，且分段清晰。它不红 |
| O7 | 按 describe 拆 `AppV2.spec.ts` | 见第 6 节。拆 8 份会把 380 行 mock 装置复制 8 份，是净损失 |
| O8 | 现在重建 v5 部署文档 | v5 期做部署文档等于用测试期的时间做 v6 的事。只保留编号与素材（ADR-070） |
| O9 | 依赖升级 / 漏洞修复 | 与测试重构正交，混进来会让本期的验收标准说不清 |

> 一条前置纪律：不做「删测试减体积」运动。 783 条用例不是负担；
> 783 条里有 34 条是假的才是负担。本期的对象是可信度，不是体积。

---

## 2. 四个判断

本节是本层的设计起点。立项时提出了四个问题，下面逐条给结论与依据。

### 2.1 Q1：当前测试是否太臃肿？是否有必要重构？

结论：不是臃肿，是失序。有必要重构，但重构的对象是可信度，不是体积。

| 工程 | 测试文件 | 测试行 | 源文件 | 源码行 | 测试行/源码行 |
| --- | --- | --- | --- | --- | --- |
| backend | 54 | 18,862 | 67 | 25,083 | 0.752 |
| frontend | 38 | 14,507 | 74 | 18,694 | 0.776 |
| frontend_admin | 12 | 2,585 | 20 | 5,838 | 0.443 |

密度偏高，但不失控。真正的四个问题全部是「这条断言到底算不算数」：

1. main 分支有 34 个已知失败，且已存在一段时间没人修。
   **triage 已定性：18 个已证为测试缺陷、0 个确证为产品缺陷、16 个待定（倾向测试缺陷）**
   （三条已证机制与源码位置见 6.2，施工工单见 `roadmap.md` T02）；
2. `test_login_lockout.py` 里 3 个用例永不执行（文件 18 个 def 只收集 15 个）；
3. `test_auth_service.py` 的邀请码原子性用例里，线程抛 `sqlite3.OperationalError`
   时 pytest 只转成 `PytestUnhandledThreadExceptionWarning`，用例照常判通过；
4. backend 全量 362.7 秒，慢到没人愿意跑，等于事实上的不可执行。

最危险的信号不是任何一个数字，而是「main 一直是红的而没人修」。 这说明套件
已经失去了否决权。一旦习惯了「红是正常的」，之后新加的每一条测试都不再被信任。

> 所以 v5 的一切动作都指向恢复权威，而不是增加用例数。

### 2.2 Q2：「后端测试很多、前端相对少」——这个印象对不对？

结论：这个印象是错的，而且问法本身也不对。分两层答。

第一层，用数据正面回答：印象错了。 前端的测试密度 0.776 略高于后端的 0.752。
产生「后端多」这个印象的是绝对文件数（54 vs 38）与后端测试在提交里更显眼，不是密度。

唯一真正失衡的是 `frontend_admin`（0.443，约为 frontend 的 57%）——
而它恰好是唯一不在 CI 里的那个。这两个事实是同一件事的两面。

第二层，更重要：密度不是正确的度量。 两个 `vitest.config.ts` 都只配了
`environment: 'jsdom'`，没有 coverage。所以 0.776 衡量的是「测试代码写了多少行」，
不是「覆盖了多少产品代码」。一个 0.776 的套件可能覆盖了 85%，也可能只覆盖了 30%。

> 因此在开 coverage 之前，「谁多谁少」这个问题无法真正回答。这是 I8 被列为
> 本期范围项、而不是可选项的原因：它是 Q2 的前置条件，不是附带收益。

### 2.3 Q3：是否应该加入集成测试、系统测试（e2e）？

#### 集成测试：加。停止线是一条可执行的判定规则

判定规则：只有当「经过 HTTP 层」本身会改变可观测行为时，才写集成测试。

| 接口类型 | HTTP 层的角色 | 结论 |
| --- | --- | --- |
| 普通 REST 接口 | 只是搬运工，测 service 就等于测了集成 | 不写 |
| SSE 流式接口 | 分帧节奏、响应头、连接语义本身就是契约，绕开 HTTP 就等于没测 | 必须写 |

按这条规则扫一遍，命中的只有 SSE 那一族：`/api/jobs/{id}/stream` 与
`conversation/*-stream`。加满这一族就停。

明确不做的反面：不给每个接口补一条 HTTP 冒烟。那是 40 多个用例，覆盖的是
TestClient 自己，不是产品。

落点：`backend/tests/integration/test_sse_transport.py`，1 个文件，≤150 行。

> 为什么是「薄」的：本项目单进程 + 单 SQLite + 单 app，没有多服务、没有消息队列、
> 没有跨进程边界。这个仓库里不存在天然的集成第二层。 硬造一个，
> 造出来的就是 HTTP 冒烟。

#### e2e：不做。但触发条件明确，且有终点

| # | 触发条件 | 当前状态 |
| --- | --- | --- |
| E1 | 仓库出现 `Dockerfile` / compose / `nginx.conf`（有了可复现的生产环境） | 未成立（零命中） |
| E2 | 出现第二个前端（移动端 / 小程序）共用同一后端 | 未成立 |
| E3 | 引入真实注册 / 支付等不可 mock 的外部依赖 | 未成立 |
| E4 | 出现第二个维护者（selector 维护成本可被分摊） | 未成立（单人） |

四条今天一条都不成立，所以不做。

> ⚠️ 注意时间关系：v5 之后紧接着就是 v6 部署，而容器化是部署的自然产物，
> 所以 E1 在 v6 就会被触发。 e2e 的落点是「v6 后期重评」，不是「永远不做」。
> 这个判断有终点，不是托词。

补充一条已被证实的事实：两个 `vitest.config.ts` 的 `exclude` 里都已经含 `'e2e/**'`，
但 `e2e/` 目录根本不存在——门留好了，房间里没人。本期不往里放东西。

### 2.4 Q4：性能、安全方面要不要加？怎么加？

结论：都加，但都只加护栏断言，不加体系。

#### 性能

| 加 | 不加 |
| --- | --- |
| 导出 50MB 上限的回归守卫（`export_service.MAX_EXPORT_BYTES` 当前后端零测试） | 不测「生成一篇 PRD 要多久」 |
| 生成并发写锁的上限守卫 | 不建 benchmark 基线 |
| 把 backend 全量耗时本身做成 CI 级护栏 | 不引 k6 / locust |

第三条是本节的关键：362.7 秒是本项目当前唯一真实存在、且每天都在发生的性能问题。
测产品性能没有靶子（仓库里没有目标机配置），测测试性能有靶子。
所以「性能」在 v5 期唯一真正落地的形式，是测试自身的耗时护栏（ADR-069）。

#### 安全

不引扫描器（O3），改为 5 条定向用例：

| # | 用例 | 补的是哪一面 |
| --- | --- | --- |
| 1 | `auth_cookie_secure=true` 的正向断言 | 当前 `test_auth_api.py` 只断言 `Secure not in set_cookie`。反向断言在配置项被删掉时也会绿 |
| 2 | `reveal-key` 越权（非管理员访问） | 已有的 reveal-key 用例守的是正常路径，这条守的是越权面 |
| 3 | 导出超 50MB → 413 | 后端零测试，前端只断言了提示文案 |
| 4 | SSE 响应头 | 与 I4 合并，不单独算（ADR-061） |
| 5 | 邀请码并发原子性用例本身 | 它目前在吞线程异常，对「并发下邀请码超发」这个真实安全问题是失明的 |

> 第 5 条要单独记一笔：修它属于安全，不属于清理。它不该记在 I6 的账上。

---

## 3. 测试分层

### 3.1 分层定义

| 层 | 职责 | 判定标准（什么样的测试算这一层） | 落点 | 现有存量归类 |
| --- | --- | --- | --- | --- |
| L1 单元 | 单个函数 / 类的逻辑 | 不碰 HTTP、不碰 DB（纯 repository 测试可用 tmp DB） | 原地不动，用 marker 标记 | `test_crypto_secrets.py`、`test_token_budget.py` 一类 |
| L2 服务 | 跨 repository + 外部适配的编排 | 走真实 SQLite（tmp）+ fake LLM，但不经 HTTP | 同上 | `test_generation_job.py` 主体、`test_clarify.py`、`test_auto_clarify.py` |
| L3 接口 | HTTP 契约：状态码 / 响应体 / 鉴权 / 错误码 | 必须经 TestClient，且断言 HTTP 层可观测的东西 | 同上 | admin 组、auth、documents、solutions、export |
| L4 集成 | 传输层本身构成契约的部分 | 经真实 HTTP 且断言响应头 / 分帧 / 流式语义 | `backend/tests/integration/` | 新增 |
| L5 e2e | 全链路 + 真实浏览器 | —— | 不存在 | 不建 |

### 3.2 现状与目标（可验证的指标，不编百分比）

| 指标 | 现状 | v5 结束目标 | 验收方式 |
| --- | --- | --- | --- |
| CI 覆盖工程数 | 2 / 3 | 3 / 3 | `.github/workflows/ci.yml` 的 job 数 |
| 已知失败 | **34（18 已证测试缺陷 / 16 待定 / 0 确证产品缺陷）** | 0 | 三工程全量绿 |
| backend 全量耗时 | 362.7s | ≤150s | CI 日志计时 |
| 集成测试 | 0 | 约 15 例 / 1 文件 | `pytest -m integration` 可单独跑 |
| e2e | 0 | 0（有意保持） | 无 e2e 目录 |
| 测试文件 marker 覆盖 | 0% | 100% | 守卫自证 |
| coverage 可观测 | 无 | 三工程均能出数 | 三个工程各自能产出报告 |
| `noqa: SLF001` 出现数 | 基线 B | ≤ B，且至少降 1 | 守卫 G2 |
| 死用例 | 5（3 + 2，T07 已清） | 0 | 守卫 G1 |

> 不设 L1:L2:L3 的占比目标。 因为不做 863 例重分类（O5），任何占比数字都是编的。
> 改为对新增用例按 60 / 30 / 10（L1+L2 / L3 / L4）分配。

### 3.3 用 marker 表达分层，不搬目录

54 个文件留在原地，每个文件顶部加一行 `pytestmark = pytest.mark.<layer>`，
四个 marker 在 `pyproject.toml` 注册。

- `pytest -m api` → 只跑 HTTP 契约；
- `pytest -m "not integration"` → 快速循环。

可行性依据：本仓库已经有静态扫描守卫的先例——`backend/tests/test_layering.py`
守「services / repositories 不得反向 import api」，而且它连自检都做了
（两个 `..._is_not_vacuous` 用例，用已知坏代码证明正则没瞎）。
这是本项目既有的 idiom，本层是沿用，不是引进新范式。

### 3.4 棘轮守卫 G1–G4

新增 `backend/tests/test_test_quality_guards.py`：

| 守卫 | 断言 | 能抓住什么 |
| --- | --- | --- |
| G1 同文件内无同名用例 | **同一文件内不得出现同名 `def test_*`**（同名必然后者覆盖前者 = 死代码） | `test_login_lockout.py` 3 条 + `test_skill_registry.py` 2 条，共 5 条死代码 |
| G2 SLF001 棘轮 | `noqa: SLF001` 出现次数 ≤ 基线常数 B | 私有成员直触只许减少不许增加 |
| G3 重名用例 | 无两个文件定义同名 `test_xxx` | 抓住 `test_migrations_reach_latest_version` 这类跨文件重复 |
| G4 时长护栏 | 单文件耗时 ≤ 阈值 | 慢测试劣化可见 |

棘轮模式是本层对单人项目最实用的一条设计：它不需要一次性清理 863 个用例，
但保证明天不会比今天更差；每做一次清理就把基线往下拧一格，拧下去就回不来。

四条守卫各自配一条自检用例（沿用 `test_layering.py` 的 `is_not_vacuous` 惯例），
确保守卫本身不会静默失明。

> ⚠️ **G1 的判据为什么是「同文件内无同名」而不是「全仓 def 数 == 收集数」**：
> 后者实测 `828 ≠ 871`（差 43 条全是 parametrize 展开），从第一天就是红的。
> parametrize 让 collected 偏大、同名覆盖让它偏小，两个相反效应在全仓总数上互相抵消，
> **永远无法区分**。收集数因此只作记录值，不作判据（详见 ADR-064）。
>
> 判据必须比它守的缺陷更稳 —— 这条经验在本层出现过三次（`dropped` 求和、
> `--durations` 读尾、G1 用总数），共同点是**都不会报错，只会安静地给出错答案**。

---

## 4. 测试替身策略

| 边界 | 用 fake 还是真货 | 理由 |
| --- | --- | --- |
| LLM | fake（`tests/fakes.py::ScriptedLLM`） | 唯一必须替掉的。真实调用烧钱、不确定、不可重复 |
| SQLite | 一律真货（tmp 文件） | 大量语义依赖 SQL 本身：JOIN、级联删除（ADR-048）、唯一约束。替掉 repository 等于缴械 |
| HTTP | L1 / L2 不用；L3 / L4 必须真货（TestClient） | 否则测的是 mock |
| 时钟 / 网络 | 需要时 fake | 渐进延时（ADR-047）与 TTL 用例需要 |

关于 `tests/fakes.py` 现在只有 1 个 `ScriptedLLM`：这是对的，别加。

治理规则：`fakes.py` 只放「外部世界边界」的替身，新增需过 ADR。
否则它会变成杂物抽屉——而一旦 fake 变多，「这个 fake 与真实行为一致吗」
本身就变成新的维护负担。目前 `ScriptedLLM` 用 `await asyncio.sleep(0)`
主动让出控制权以贴近真实节奏，这种讲究只有在 1–2 个 fake 时才维持得住。

---

## 5. backend 提速路线

### 5.1 现状

backend 全量 863 例耗时 362.7 秒。怀疑主因是 `tests/conftest.py` 的
`isolated_db` 是 autouse 夹具，每个用例都做：

1. `tmp_path` 建目录；
2. `Fernet.generate_key()` 生成主密钥并写文件；
3. `init_schema()` 建全表（20 余条 DDL，WAL 模式下每条都要落盘）；
4. 用例结束后 `_reset_runtime_state()`。

共 863 次建库。但这条结论是推断，未经 profiler 验证。

> **验证结果（T04 已执行，结论推翻）**：`init_schema` 实测 26.7 ms/次，
> × 867 = 23.1 秒，**只占 8%**，与 362 秒不同量级。真热点是 `client` 夹具
> 每用例净增 147 ms（`init_schema` 被调两次 51.6 + `run_migrations` 25.2 +
> 关日志线程 50.6 + 其余 13.7）。因此**没有改 conftest**，改为默认并行执行：
> 全量从 283 秒降到 **72 秒**，收集数与逐条结果均与单进程一致。
> 详见 `../../adr/README.md` 的 ADR-071。

### 5.2 测量先行：改任何东西之前必做的两条命令

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

- 若实验 B 的结果与 362 秒同量级 → 假设成立，走 5.3 的方案甲；
- 若只占零头 → 假设被推翻。改用实验 A 的 `--durations` 找真正的热点，
  别在 conftest 上白费力气。

> 前置事实（供判读）：迁移共 20 步（v1–v20），而 `conftest.py` 只调 `init_schema()`、
> 不跑迁移——这一点 `core/migrations.py` 自己的注释里写明了。
> 它直接决定了方案甲的安全性论证。

### 5.3 方案甲：模板库 + 文件复制

session 级夹具只建一次库，之后每个用例把模板 `shutil.copy` 到自己的 tmp 路径。

安全性论证（这是本方案成立的关键）：

1. `init_schema()` 全是 `CREATE TABLE IF NOT EXISTS`，产出的是一个空库；
2. conftest 本来就不跑迁移；
3. 因此「复制一份空库」与「现建一份空库」行为完全一致，零行为变更。

两条实现注意：

- WAL 注意事项：模板必须在连接关闭之后再复制。这里恰是 `cp` 安全的场合
  （新建即关、无在途写入），与既有认知「备份不能 `cp`、要用 `VACUUM INTO`」不冲突
  ——后者针对的是有在途写入的库。
- 保留每用例一把 `Fernet.generate_key()`：成本约 10 微秒可忽略，
  且它保证密钥用例互不串味。

### 5.4 明确否决的方案乙

把 `isolated_db` 改成 session 级、全套件共用一个库。
这等于直接制造 conftest 文件头自己写着的那个问题（原文：否则用例之间会互相
看见对方注册的用户、核销掉的邀请码，测试结果随执行顺序漂移）。提速再多也不换。

### 5.5 验收标准

> 改动前后，`pytest --collect-only -q` 的总数必须完全相同（**当前 867**），
> 且 pass / fail 集合逐条一致（当前基线是 **867 全绿**）。
>
> 这两项任一不满足，提速视为失败并回滚。

这条是本层最值钱的一条验收标准。理由：「快了但少跑了 5 个用例」是这类夹具改动
最容易犯、且只看耗时完全看不出来的错。 只看耗时会让这类改动以「优化成功」的姿态
通过验收，而实际是静默地削弱了套件。

---

## 6. 前端共享状态治理

### 6.1 一处确凿的泄漏，但实测只解释 15 个里的 1 个

`frontend/src/__tests__/AppV2.spec.ts`（2,991 行 / 83 个 `it`）是当前最红的文件（15 个失败）。
其中存在一处确凿的共享状态泄漏：

```js
// 第 49 行
const turnHarness = vi.hoisted(() => ({ items: [] as unknown[] }))   // ← 从不重置
// 第 51 行
const jobHarness  = vi.hoisted(() => { /* ... */ reset() { kinds.clear(); nextId = 1 } })

// 第 382–384 行
beforeEach(() => {
  jobHarness.reset()   // 有重置
  vi.clearAllMocks()   // 只清 mock 的调用记录，清不掉普通数组
})
```

两个 harness 的重置是不对称的：`turnHarness.items` 是普通数组不是 mock，
`vi.clearAllMocks()` 对它无效；而第 193 行的共享 helper 会往里 `push`。
`jobHarness` 专门写了 `reset()` 方法（第 51–67 行），说明作者清楚跨用例泄漏这件事，
只是给 `turnHarness` 漏了。

> ⚠️ 但它的危害范围经实测只有 1 个用例，不是 15 个。
> QA 把 15 个失败逐个单独跑（`-t <name>`，其余 82 个 skipped）：
> 补上这一行能让「对话完成后可生成 PRD…」转绿，其余 14 个在完全隔离下照样红。
>
> 这一行该修（它是真 bug，不修会永久污染后续用例），
> 但不能当成 15 连红的收尾动作——否则会修好 1 个、剩 14 个照红，
> 然后误判「AppV2 的问题已经解决」。

还有一条更值得注意的观察：那条 `expected [ …(5) ] to deeply equal [ …(1) ]`
的失败在单用例隔离下也复现，说明该数组的膨胀在单个用例内部就发生了
（第 193 行的 helper 在一次用例里被触发多次）。
这意味着它缺的不只是重置，还有上限或语义约束——重置只是止血，不是根治。

### 6.2 T00 结论：18 已证测试缺陷 / 16 待定 / 0 确证产品缺陷

T00 已执行完毕。实验方法是逐个 `-t <name>` 单跑（同文件其余用例全部 skipped），
结论**推翻了「顺序依赖」这个方向**：

| 检查 | 结果 |
| --- | --- |
| 跨文件顺序依赖 | **排除**。6 个文件单独跑 vs 全量跑，失败数逐项完全相同（15 / 8 / 5 / 2 / 2 / 1，合计 33） |
| AppV2 内部 | 15 个逐个单跑：**仅 1 个转绿，14 个仍红** |
| 5 个 store 文件 | 18 个逐个单跑：**18 个全部仍红** |

连带修正两处数据：`workflow-store` 的失败数是 **5**（初稿记为 2），
store 五文件合计 **18**（初稿记为 15）；六个文件合计 33 与总数吻合。
另外「不是 Pinia 单例残留」这一排除结论成立——五个 store 文件全部在
`beforeEach` 里做了 `setActivePinia(createPinia())`。

**triage 最终定性：34 个失败 = 18 个已证测试缺陷 + 16 个待定（倾向测试缺陷）+ 0 个确证产品缺陷。**

三条已证机制（均有源码位置与引入提交，是 T02 施工的直接依据）：

| # | 机制 | 影响 | 源码位置 | 引入提交 |
| --- | --- | --- | --- | --- |
| ① | `chatStage` 新字段未同步进测试期望 | 3 条（`runs-store` 1 + `solution-switch-isolation` 2） | `src/stores/runs.ts:75` 声明、`:125` 写入 pointer | `c155608` 引入、`fddad49` 完善 |
| ② | `solutionService` 的 mock 工厂漏列 `fetchTurns` | 可解释 `solutions-store` 的 **5~8 条** | `fetchTurns` 定义在 `src/services/solutionService.ts:176`，被 `src/stores/solutions.ts:35` 导入、`:457` 与 `:526` 调用；`solutions-store.spec.ts:28-40` 的 mock 工厂只列 6 项 | —— |
| ③ | `fddad49` 是有意的语义变更，测试没跟上 | 5 条（`workflow-store` 4 + `orphan-solution` 1） | `hasContent` 不再看 `messages`、`contentTick` 的 watch 清单去掉 `messages`、`snapshot` 不再导出 `messages`；新增 `hasChat` 承接原语义 | `fddad49` |
| ④ | backend 那 1 条：跨用例污染 | 1 条 | `backend/tests/test_admin_logs.py::test_pagination_reports_total_not_page_size` | —— |

四条的关键证据都写清楚，别让后人重推一遍：

- ① 的反证最有说服力：**`useClarificationChat.spec.ts` 里有 14 处在用 `chatStage` 且是绿的**
  ——说明 `chatStage` 本身是产品正常特性，只是这两个 spec 的期望对象没更新。
  而 `7d3d9f1` 只给这两个 spec 各补了 **+1 行**，补丁没打全。
- ② 的后果链：`openSolution()` 调 `fetchTurns` → 抛错 → 被吞 → 返回 false →
  4 条 `expected false to be true` + 1 条 `to contain '损坏'` 集体红。
- ③ 尤其要注意：**作者确实同步过测试**（`fddad49` 同时删了 `workflow-store.spec.ts` 47 行），
  **但没删干净**。所以这不是「没人管测试」，是「改语义时漏了一部分」。
- ④ 单独跑整个文件 10 passed、单独跑该用例也过，**全量跑才红** → 跨用例污染/顺序依赖。
  ⚠️ 注意 `conftest.py` 的 `isolated_db` 是 autouse 的，
  所以**污染源多半不是数据库，而是模块级或缓存状态**——需施工阶段二分定位。

> 16 条待定（AppV2 的 14 条 + `workflow-store` 1 条 + `orphan-solution` 1 条）
> **不要写死为测试缺陷**。QA 已排除 mock 漏列这条路（核对过 `AppV2.vue` 从 api
> 只导入 `fetchActiveJobs` / `onUnauthorized`，两者都已 mock），但拿不出单点机制。
> 定性是「**倾向测试缺陷，施工阶段逐条确认**」。

### 6.3 处置顺序：先 triage，再止血，然后抽装置，拆文件降为可选

本层不按 describe 拆 `AppV2.spec.ts`（与 QA 初稿结论相反），三条理由：

1. 它不是真的臃肿，它已经分好层了。 2,991 行里有 8–9 个现成的 describe 块
   （完整流程编排 / 文档手改自动落库与下载 / 入口模式 / 分章节生成的阶段展示 /
   文档澄清 / 对话页的子标签栏 / 版本历史抽屉 / 版本预览与删除）。
   内部结构是清晰的，不好的是共享状态的管理，不是文件的划分。
2. 按 describe 拆成 8 个文件，会把第 34–400 行那 380 行 mock 装置复制 8 份。
   以后改一次装置要改 8 个文件——这是把「一个大文件」换成「八处重复」，净损失。
3. 真正的修法是把装置抽出去，而不是把用例摊开。 仓库里已经有先例：
   `frontend/src/__tests__/support/solutionState.ts`。

所以顺序是：

| 顺序 | 动作 | 时期 |
| --- | --- | --- |
| ① | 按三条已证机制改测试（18 条，含 `turnHarness` 那一行止血——实测只解决 1 个） | T02 |
| ② | 16 条待定按「先改测试 → 仍红再回头查源码」分流；确证为产品 bug 的另立 v5.0.5 | T02a / T02 |
| ③ | 抽 `support/appV2Harness.ts`（导出两个 harness + 全部 `vi.mock` + `installDefaults()`） | T11 |
| ④ | 按 describe 拆文件（P3，可不做） | v5.x 之后 |

> 顺序的要点是 ② 必须守住「**先改测试 → 仍红再查源码**」这个方向：
> 如果默认全部改测试，就会把真实回归「修」成绿灯——那比红着更糟，
> 因为它从此不再有任何信号。
> 反过来，跳过 ① 直接处理 ② 也不对：18 条已有机制，先改掉能缩小待定集合。

第 ③ 步为什么值得做：那 380 行 mock 装置被 83 个用例共享，
任何一次改动的爆炸半径是 83。抽成模块后这 380 行有了单一归属，
且 Vitest 的模块隔离边界是文件，每个测试文件拿到独立的 harness 实例。

> ⚠️ 但要写清楚别让人误读：抽成模块只解决跨文件，
> 同一个文件内 83 个 `it` 之间仍然共享装置实例，
> 所以 `reset()` 仍然必须显式写在 `beforeEach` 里。抽完不等于自动免疫。

第 ④ 步唯一额外的好处是文件级并行（8 个文件 = 8 个并行单元，能实打实砍
frontend 的 43.2 秒）。想要并行时才做，否则不做。
它对解决当前这 34 个失败一点帮助都没有——那些失败既不是状态泄漏，也不是文件太大。

---

## 7. SSE 传输契约

### 7.1 契约的两端

| 端 | 位置 | 现状 |
| --- | --- | --- |
| 服务端 | `backend/src/backend/api/sse.py` 定义 `SSE_HEADERS`（`Cache-Control: no-cache`、`Connection: keep-alive`、`X-Accel-Buffering: no`）+ `sse_response()` | 存在 |
| 客户端 | `frontend/src/services/api.ts::readStreamRequest`，用 `fetch` + `response.body.getReader()`（非 EventSource），`Accept: text/event-stream`，`credentials: 'same-origin'` | 存在 |
| 测试 | 后端 `tests/` 全目录 grep 三个响应头 → 零命中 | 缺失 |

前端侧 `api.spec.ts` 用 mock `ReadableStream` 测了跨块半帧拼接，这部分是够的。
缺的是后端真的把头发出去、以及真实分帧节奏。

### 7.2 契约内容（T05 要守的五条）

| # | 断言 |
| --- | --- |
| 1 | `Content-Type: text/event-stream` |
| 2 | `Cache-Control: no-cache` |
| 3 | `X-Accel-Buffering: no` |
| 4 | `Connection: keep-alive` |
| 5 | 真实分帧：跨 chunk 的半帧能拼回完整事件，且流以 `[DONE]` 结尾 |

### 7.3 这条契约守到哪为止（写清楚，别以后当成 bug）

它守的是「后端发出的头是对的」，不是「生产链路是通的」。
要验证后者需要 nginx 配置进入版本控制，而当前仓库里 `Dockerfile` / `nginx.conf`
零命中。所以：

- 真实断线重连在 jsdom 下不做自动化覆盖（jsdom 无真实网络中断）；
- 生产链路的验证留到 v6 容器化之后。

---

## 8. 取舍与风险

### 8.1 取舍登记

| 议题 | 取 | 舍 | 记录 |
| --- | --- | --- | --- |
| 分层的容器 | marker + 静态守卫 | 物理目录隔离；分层是约定而非强制 | ADR-060 |
| 集成测试范围 | 只覆盖 SSE 一族 | 每个接口的 HTTP 冒烟 | ADR-061 |
| e2e | 不建，触发条件命中才评 | 跨端自动化回归 | ADR-062 |
| 性能 / 安全 | 护栏断言 + 定向用例 | 压测体系与扫描器 | ADR-063 |
| 存量清理 | 棘轮守卫 | 一次性 863 例大扫除 | ADR-064 |
| AppV2 处置 | 修一行 → 抽装置 → 拆文件可选 | 立即按 describe 拆分 | ADR-065 |
| backend 提速 | 测量先行 + 对拍验收 | 只看耗时的验收 | ADR-066 |
| coverage | 只看不设阈值 | 立刻设百分比门槛 | ADR-067 |
| v5 部署文档 | 编号保留 + 素材留待 v6 | 现在重建 / 原地改写 / 重编号 | ADR-070 |

### 8.2 风险登记

| # | 风险 | 缓解 |
| --- | --- | --- |
| **R0** | **16 条待定（主要是 AppV2 的 14 条）是唯一一批拿不出只读证据的，可能藏着 1~2 个真实回归** | **施工严格按「先改测试 → 若仍红再回头查源码」的顺序**。不要默认全部改测试——否则会把真实回归「修」成绿灯 |
| R1 | 动 `conftest.py` 的 autouse 夹具导致用例互相污染，且只看耗时看不出来 | 对拍验收是硬门槛：收集数 867 不变 + pass/fail 集合逐条一致。任一不满足即回滚 |
| R2 | T02 的工程量从「改 1 行」放大到「三批 18 条 + 16 条逐条确认」 | T02 拆两步执行（先已证的 18 条，再 16 条待定），每步单独验收。**若 16 条里最终确证出产品 bug，另立 v5.0.5**——triage 结论是 0 个确证，但不排除施工期翻出新的 |
| R3 | T07 删掉不该删的用例（3 个死用例的判断基于重名分析，未必全是死代码） | 逐条删，每删一条立刻对拍收集数，确认少的正好是那一条 |
| R4 | 新写的用例本身是瞎的（永远绿） | 每条新用例必须做反向验证：临时破坏被测行为 → 观察转红 → 还原。这是本项目既有纪律 |
| R5 | T09 铺 54 个文件的 marker 时漏铺 | 用守卫自证：断言无 marker 的测试文件数为 0 |
| R6 | T12 摘录的 v6 素材与原始决策有出入 | 只摘录不重判，并注明来源是内存日记 |
| R7 | 本期做完，测试又慢慢变红（治标不治本） | 这才是 R1–R6 之上的真风险。缓解物是三条：CI 时长护栏（ADR-069）+ 棘轮守卫（ADR-064）+ CI 三个 job 全开 |

### 8.3 如果只能做一件事

做 T01：把 `frontend_admin` 加进 CI。

1. 10–15 行换 114 个用例从「没人跑过」变成「每次 push 都跑」
   ——这是整个 v5 里投入产出比唯一一个数量级领先的。
2. 它零风险：admin 本地已经 114 / 114 全绿，进 CI 只是让这个事实被人看见，
   不需要修任何东西。
3. 它修的是最具体的那个洞：114 个用例是已经写好的资产，
   现在的状态是资产闲置。其他所有任务都是「让资产变好」，
   只有这个是「让已有的资产开始工作」。

> 注意两条已被实测推翻的初稿判断：
>
> 1. 初稿曾说「补 `turnHarness` 那一行，直取 15 个失败」——**这是错的**，
>    实测它只解决 1 个（见 6.1）。
> 2. 初稿把 T02 设想成「改 1 行 + 修 5 个文件」——**工程量被严重低估**，
>    实际是三批 18 条 + 16 条逐条确认（见 6.2）。
>
> 第二件应改为 **T02 的第一步**：按三条已证机制改那 18 条。
> triage 已完成，这 18 条现在带源码位置与引入提交号，是可以直接派工的。
>
> 而「如果只能做一件但必须选最难的那种」，那是 T04 的提速：
> 因为它把 863 个用例从「一天跑一次」变回「随手就跑」——
> 套件能不能重新被信任，最终取决于人跑不跑它。

---

## 9. 与 v4 的关系

- v5 不改 v4 定型的任何业务行为，ADR-039~053 全部继续有效。
- 对三个工程的产品代码零侵入；本期动的全部是测试侧资产
  （CI 编排、conftest、测试用例、测试配置）。
- v4 期引入的横切关注点（运行时配置 ADR-041、密钥加密 ADR-053），
  其配套测试在本期 T06 补齐——v4 期 `c5dee02` 一笔提交动了 19 个测试文件，
  说明这类横切关注点缺少收口的守卫。

> 与 v6 的交接有两条待办，已在本层挂号（详见 `../../adr/README.md` ADR-062 与 ADR-070）：
> v6 期重评 e2e、v6 期按原号重建部署 ADR。

---

## 附录 A：性能面板（原 T14）

> 迁自 `T14-performance-panel.md`（原文件已删除，内容逐字保留）。

# T14 · 接口性能监测面板（管理员端）

> **一句话**：让管理员在后台直接看到「哪个接口慢、错多少、数据到底可信到什么程度」——
> 复用已有的访问日志做纯查询，**不新增采集、不新增一次写放大**。
>
> 上层：`README.md`（本层索引）、`roadmap.md` 的 T14 节（排期与验收）、`../../adr/README.md`（决策）
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

---

## 附录 B：数据访问层收口（原 T15）

> 迁自 `T15-data-access-layer.md`（原文件已删除，内容逐字保留）。

# T15 · 数据访问层：`Database` 对象 + 单元事务

> 状态：**已被 v6 取代，且 v6 已完成**（2026-10-23，1001 例全绿）。对应决策见 `../../adr/README.md` 的 ADR-072。
> ⚠️ 本文描述的 444 行自建连接池、`lease()` / `owned` 闸门、手写 `SCHEMA_SQL` 与
> `PRAGMA user_version` 迁移链，在 **v6（`docs/architecture/v6/`，ADR-073~082）** 中
> 全部交由 SQLModel + SQLAlchemy + Alembic 承担。最终落地状态：`core/database.py`、
> `core/db.py` 的 `connect`/`lease` 已删除；`SCHEMA_SQL` 与 `init_schema()` 作为冻结链的
> v0 起点搬进 `core/migrations.py`，生产启动只走 `prepare_schema`（守卫 C9）。
> T15.3 冻结在 batch A 的那 5 个文件已在 T16.6 完成迁移。
> 本文保留为历史记录：**它记录了「为什么当时的做法是对的」，这个判断在 2026-10 之前一直成立。**
> 读本文时请把「目标形态」当作历史方案，不要照着实现。

## 1. 目标与范围

三句话目标：

1. 让「多线程同时首次映射 WAL `-shm`」这个故障**在结构上不可能发生**，而不是靠一把锁把它挡住；
2. 把事务边界从「一个仓库函数」上移到「**一次业务操作**」，让跨仓库写入具有原子性；
3. 把「用例之间的数据库隔离」从「改全局环境变量 + 清一堆缓存」变成「**换一个 `Database` 实例**」。

### 本期做

| # | 内容 |
| --- | --- |
| 1 | 新增 `core/database.py`：`Database` 对象 + 固定连接的连接池 |
| 2 | `db.connect()` 改为委托 `Database.acquire()`（对外签名不变） |
| 3 | 归还连接时强制回滚未提交事务，并计入统计 |
| 4 | 仓库函数支持「传入连接」，服务层据此拥有事务边界 |
| 5 | 删除 `db._bootstrap_lock`，改由「连接只在 open 时创建」保证不变量 |
| 6 | 五条确定性守卫（见第 6 节） |

### 本期不做

| 不做 | 理由 |
| --- | --- |
| 换 SQLAlchemy / 换 Postgres | SQLite 单写者是本项目的既定边界；换存储是另一个量级的事，不属于「访问层封装」 |
| 异步驱动（aiosqlite） | 现有调用链全是同步函数（服务层在 `run_in_threadpool`/任务线程里跑），换异步驱动要改调用方语义，收益不明 |
| 拆 `repositories/` 目录 | 目录结构不是本期的痛点，事务边界才是 |
| 改 lifespan 的 `init_schema + run_migrations` | 生产上本来就只跑一次；测试侧的开销属于测试装置问题（T04 已用并行解决），不为此改生产启动路径 |
| 连接池动态伸缩 | 见 3.2：**连接数恒定**是「竞态不可能发生」的前提 |

## 2. 现状事实（均已实测，不是推断）

| 事实 | 数值 / 位置 |
| --- | --- |
| `with connect()` 使用点 | **92 处**：`repositories/` 89、`core/` 2、`api/` 1 |
| 直接开事务的公共仓库函数 | **89 个**（88 公共 + 1 私有）；公共里 **43 写 / 45 只读**。写函数里共 44 处 `.commit()`、2 处 `rollback()`（全在 `document_repo`） |
| 仓库内显式 `.commit()` | **44 处**（auth 16、generation_job 9、runtime_config 5、其余 14） |
| 共用的 `connect()` 实现 | `core/db.py:279`，每次操作新建连接 + 3 条 PRAGMA，`yield` 后关闭 |
| 单次 `connect + SELECT 1 + close` | **1.98 ms**（p50 1.93，300 次；预热后） |
| 路径解析 | `db_file_path()` **每次调用**读 `get_settings()`，所以路径可在运行期变化 |
| 测试隔离方式 | `conftest.py:39` 每个用例 `DB_PATH = tmp_path/test.db` |
| 已有的正确做法 | `document_repo.append_version`（`repositories/document_repo.py:171`）在一个 `with connect()` 里完成四步写入，并把 `conn` 传给 `_ensure_document(conn, ...)` |
| 事务边界止步于 | 仓库函数：服务层调两个仓库 = 两个事务 |

**最后两行是本期设计的前提**：连接注入（`conn` 作为参数往下传）在本项目里**已经是局部惯例**，本期只是把它提升为全局约定。

## 3. 设计

### 3.1 组件关系

```
services/            ← 拥有事务边界：with db.transaction() as tx
   │                    一次业务操作 = 一个事务（可跨多个仓库）
repositories/        ← 只写 SQL；函数接受 tx（缺省自己开）
   │
core/database.py     ← Database：持有连接池、路径、PRAGMA、生命周期、统计
   │
sqlite3              ← WAL
```

`db.connect()` 保留为兼容入口（92 处调用点一行不用改），内部改为委托：

```python
@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    with get_database().acquire() as conn:
        yield conn
```

### 3.2 连接池：数量恒定、只在 open 时创建

这是整个方案的核心决策，也是「可以删锁」的**唯一**依据。

```
Database(path, size=4)   # 可用 DB_POOL_SIZE 覆盖
  .open()      ← 启动期、单线程：一次性建满 size 条连接（WAL -shm 只映射这一次）
  .acquire()   ← 借出 / 归还，期间不创建任何新连接
  .dispose()   ← 全部关闭（路径切换、测试 teardown）
```

三条不变量：

1. **连接总数恒定**（`size` 条），运行期不扩容、不新建；
2. 因此「多线程同时首次映射 `-shm`」**没有发生的时机**——竞态消失，而不是被锁挡住；
3. 连接以 `check_same_thread=False` 创建（现状已如此），借出期间**独占**，不允许多人同时用同一条。

为什么不做动态伸缩：一旦允许运行期扩容，扩容那一刻就是「多线程建连接」，又把竞态请回来了；要么给扩容加锁（等于锁没删），要么预建满。预建 4 条 SQLite 连接的代价可以忽略，所以选预建（默认 `DB_POOL_SIZE=4`）。

连接损坏（磁盘错误导致连接不可用）时的替换是唯一例外：走 `_replace_locked()`，拿一把只在故障路径上的窄锁。这条路径不在热路径上，且会被记进 `stats["replaced"]`。

#### 两条实现约束（否则守卫会假绿）

1. **`connect()` 必须作为可注入的工厂参数**：`Database(path, size=..., connect_factory=sqlite3.connect)`。
   测试靠它注入计数器。⚠️ **`core/database.py` 里禁止写 `from sqlite3 import connect`**——那样
   monkeypatch 会落空、计数恒为 0，而 `0 != size` 恰好"看起来通过"，是最难发现的假绿。

2. **`open()` 必须先于任何 `connect()` 发生**。启动链路里 `init_schema()` 与
   `logging_setup.install_log_persistence()` 内的 `LogRepository.prune()` 都会走 `connect()`，
   任何一个先跑，就会触发"第一次借用时才建连接"，直接破坏「只在 open 时创建」这条唯一依据。
   做法：让 `get_database()` 在创建实例时**立即 `open()`**（在初始化锁内完成），
   这样"谁先谁后"不再依赖调用顺序。

#### `dispose()` 的语义（必须写死，否则必偶发）

`dispose()` 置 `closed` 标志 → 关闭池内空闲连接 → **不等待在途借用**。配套三条：

| 场景 | 行为 |
| --- | --- |
| 在途连接归还到已 `closed` 的池 | 直接 `conn.close()`，**不 put 回池**（否则池被复活） |
| `closed` 之后调用 `acquire()` | 抛 `PoolClosed`（不是 `PoolExhausted`、更不是自动重开） |
| teardown 撞上后台日志线程仍在借用 | 不阻塞等待；该连接在归还时按上一条直接关闭 |

#### 另外三处实现细节（文档原先没写，实现时需要）

1. **`open()` / `dispose()` 自带生命周期锁**。因为「未打开时借用」这条兜底路径（见下）可能被
   多线程同时撞上；没有锁就会出现两个线程各建 `size` 条、后一批在 `put_nowait` 上撞 `queue.Full`。
2. **`Database.closed` 属性**：G-DB3 要断言"旧池已 dispose"，靠 `stats["idle"] == 0` +
   `closed` 两处才说得清。
3. **路径解析放在 `database.default_db_file()`**，`db.db_file_path()` 委托它。
   依赖方向是 `db → database`，反过来 import 会成环；对外签名、返回值与 `mkdir` 副作用不变。

#### `acquire()` 在池未打开时怎么办

**抛 `PoolClosed`，不要「补一次 `open()`」**。理由：

- 那会让 `dispose()` 之后仍能被静默复活——`reset_database()` 做完、若还有后台线程调用
  `connect()`，就会为**已删除的临时库**重新建 4 条连接并重建文件，测试清理随之失效；
- 生产路径不受影响：`get_database()` 一定先 `open()`。

> 注意：**加了生命周期锁不等于可以不删这条兜底**。锁只解决"并发建两份"，
> 不解决"关闭后又被复活"。守卫 G-DB3 与 `test_acquire_on_closed_pool_raises` 各守一头。

#### 已知行为变化（不是缺陷，但要写进 `connect()` 的 docstring）

池化之后 `with connect() as conn:` **不再在退出时关闭连接**（改为归还）。因此
"在 `with` 块外继续使用 `conn`" 不会再立刻抛 `sqlite3.ProgrammingError(closed)`，
而是可能读到一条已归还、正被他人使用的连接。全量对拍 0 差异说明现有调用点不依赖旧语义，
但这是一个**悄悄变宽松**的地方，必须在 docstring 里写明。

### 3.3 借出与归还

```
acquire():
    conn = pool.get(timeout=acquire_timeout)      # 池空则等待，超时抛 PoolExhausted
    broken = False
    try:
        yield conn
    except sqlite3.Error:                          # 连接可能已坏（磁盘 / 句柄）
        broken = True
        raise
    finally:                                       # ⚠️ 必须 finally，不能 else
        if conn.in_transaction:                    # 调用方漏了 commit / 抛了异常
            conn.rollback()
            stats["rolled_back"] += 1
        if 池已关闭 or 代次不符:
            conn.close()                           # 绝不放进新池
        elif broken:
            _replace_locked(conn)                  # 故障路径，窄锁补一条
        else:
            pool.put(conn)
```

⚠️ **必须是 `try/finally`，不能写 `try/except/else`**：块里抛出的异常若不是 `sqlite3.Error`
（`ValueError` / `KeyError` / `CancelledError`…），`except` 不匹配、`else` 也不执行 ——
**连接一次都不会归还**。实测：`size=1` 时抛 1 次 `ValueError` 就 `in_use=1 / idle=0`，
下一次借用立刻 `PoolExhausted`；默认 `size=4` 意味着 **4 次普通异常之后全站不可用**。
这是本模块最容易写错、也最难从症状反查的一处。

**不做归还时的 `SELECT 1` 探针**：那会给每次操作加一次往返（约 0.1ms × 全部 DB 操作），
而它能发现的故障（连接损坏）在"使用中抛 `sqlite3.Error`"这一条上已经能捕获，代价却是常驻的。

**归还前回滚是硬要求**，不是优化项：

- 现状是「每次新建连接」，所以「漏 commit」的影响被限制在一次调用内；
- 池化复用后，未提交事务会跟着连接回到池里，下一个借用者可能看到脏数据、或持锁不放。

### 3.4 事务边界：`UnitOfWork`

```python
# services/ 里
with db.transaction() as tx:          # 一个连接 + 一个事务
    job_repo.finish(tx, job_id, ...)
    document_store.commit_generated(tx, ...)
```

仓库函数统一改成「可传入连接」：

```python
def append_version(..., conn: sqlite3.Connection | None = None) -> int | None:
    with lease(conn) as (c, owned):   # 传了就用传入的；没传就自己开（旧调用点不受影响）
        ...
        if owned:                     # 自己开的连接 = 提交由自己负责
            c.commit()
```

`lease()` 是新建的小工具（`core/db.py`）：`yield (conn, owned)`。传了 `conn` 就 `owned=False`
——**不提交、不回滚**，事务边界归外层；没传就 `owned=True`，调用方写完之后自己 `commit()`。

为什么用 `owned` 布尔量而不是「传没传连接再判一次」：后者在函数中途被重新赋值之后就不可靠，
读代码的人还得回头找默认值才知道这一段在不在事务里。而 `if owned:` 是可以被 AST 静态守卫的
（`tests/test_database_pool.py::test_every_repo_commit_is_guarded_by_owned`），
「漏改一处」不再依赖人工过一遍。

**只读函数也要接受 `conn`**：不只是为了将来可能加写操作。`finish` 写完 → `get` 读回来，
若后者另开连接，WAL 下会读到**提交前**的旧值（WAL 的隔离是连接的隔离，不是进程的）。
「刚生成完就查不到正文」这类故障最难查，所以 45 个只读函数一并迁移。

这样 89 个仓库函数（43 写 + 45 只读 + 1 私有）可以**逐个迁移**，不必一次性改完；
未迁移的继续走「自开事务」，行为不变。

### 3.5 错误映射与重试

| `sqlite3` 错误 | 语义 | 处理 |
| --- | --- | --- |
| `SQLITE_BUSY` | 短暂争用 | 有界重试（上限 3 次，10/20/40ms 退避）；超限抛 `DatabaseBusy` |
| `SQLITE_READONLY` | IO / 部署错误，重试无用 | **不重试**，立即抛 `DatabaseReadOnly` |
| 其余 `OperationalError` | 未知 | 原样上抛，不吞 |

现状的问题正是这两类被混在一起看，才让「调大 `busy_timeout` 完全没用」这件事看起来像谜。

### 3.6 可观测性

`get_database().stats` 暴露：

| 字段 | 用途 |
| --- | --- |
| `size` / `in_use` / `idle` | 池是否被打满 |
| `wait_ms_total` / `wait_ms_max` | 借不到连接的时间（真实瓶颈信号） |
| `rolled_back` | 调用方漏提交的次数——**这是个告警指标**，正常应恒为 0 |
| `replaced` | 连接损坏替换次数 |
| `pool_exhausted` | 超时次数 |

这些字段后续接进 T14 的接口性能面板，让「池该多大」变成数据问题。

## 4. 关键取舍

| 决策 | 选择 | 否决的方案与理由 |
| --- | --- | --- |
| 连接数 | **open 时预建满、运行期恒定** | 动态伸缩：扩容那一刻重新引入竞态，等于锁没删 |
| 池实现 | 自研（`queue.LifoQueue`） | SQLAlchemy `QueuePool`：为 4 条 SQLite 连接引入一个 ORM 生态，依赖与心智成本都不对等 |
| 事务 API | `db.transaction()` 上下文管理器 | 装饰器式 `@transactional`：无法表达「在事务里传连接」这一层 |
| 仓库兼容 | `conn=None` 缺省自开 | 一次性改签名：92 处调用点全断，且无法分批验收 |
| 归还策略 | 强制 `rollback` | 只清理 `commit` 成功的：漏提交正是要防的那种 |
| 锁的去留 | **删掉**，由恒定连接数保证 | 保留锁 + 池：锁已经没有保护对象，留着会让人误以为并发引导仍可能发生 |

## 5. 落地顺序

每一步独立可验收，**上一次全绿才做下一步**。

| 编号 | 内容 | 落点 | 依赖 |
| --- | --- | --- | --- |
| **T15.1** | `Database` + 池 + `connect()` 委托 + **同一步删锁**；守卫 G-DB1 / G-DB3 / G-DB5；`conftest` 接入 `database.reset()` 与 teardown 断言 | 新增 `core/database.py`；改 `core/db.py`、`tests/conftest.py`；新增 `tests/test_database_pool.py` | — |
| **T15.2** | 归还回滚 + 统计；守卫 G-DB2 | `core/database.py` | T15.1 |
| **T15.3** | 仓库改「可传连接」（**89 个**函数：43 写 + 45 只读 + 1 私有；分批 A/B/C） | `repositories/*.py` | T15.2 |
| **T15.4** | 服务层事务收口（跨仓库原子）+ 跨仓库回滚用例 | `services/job_service.py`、`services/document_store.py` 等 | T15.3 |
| **T15.5** | 静态守卫 G-DB4 + 全量对拍收口；**并记录「删锁前后 `PytestUnhandledThreadExceptionWarning` 出现次数」各一次** —— 作为「那把锁到底有没有用」的观测证据（**观测，不是判据**） | `tests/test_database_pool.py` | T15.4 |
| **T15.6** | 文档与记忆同步 | `docs/architecture/v5/*`、`../../adr/README.md` | T15.5 |

> 顺序的红线不是「锁必须最后删」，而是**「删锁」与「连接只在 `open()` 时创建」必须在同一步完成**。
> T15.1 建池的那一刻，锁就已经没有保护对象了；把它留到后面四步，是一段**没人调用的死代码**，
> 比删掉更危险（后来者会以为并发引导仍可能发生，从而在别处重复加锁）。
>
> 另一条红线：**T15.2 必须先于 T15.3**。没有「归还即回滚」，池复用就会把未提交事务
> 传给下一个借用者——那比偶发 500 更难查。

## 6. 验收标准

### 6.1 功能与回归

| 项 | 要求 |
| --- | --- |
| 收集数 | 与 T04 基线一致（**867 + 本期新增**） |
| pass / fail 集合 | 与基线逐条一致（`--junit-xml` 对拍，不认总数） |
| 全量耗时 | ≤150 秒（T04 的验收线不得回退） |
| 前端 / 管理端 | 不受影响，但提交前各跑一次确认 |

### 6.2 五条确定性守卫

都不依赖「复现那个 READONLY」，只守我们选择依赖的不变量。

| # | 用例 | 断言 | 反向验证 |
| --- | --- | --- | --- |
| G-DB1 | `test_bootstrap_happens_only_at_open` | **分两段计数**：① `open()` 阶段建的条数 == `size`；② 清零后 10 线程各借 50 次，新建条数 == **0** | 改成懒建 → 第 ② 段计数 > 0 → 红 |
| G-DB2 | `test_checkout_rolls_back_leaked_transaction` | 借连接 INSERT 不提交就归还；下一位借用者读不到该行，且 `stats["rolled_back"] == 1` | 去掉归还回滚 → 读到脏行 → 红 |
| G-DB3 | `test_pool_is_keyed_by_db_path` | 切 `DB_PATH` 后借到的是新库的连接，旧库行不可见 | 去掉路径比对 → 读到旧库 → 红 |
| G-DB4 | `test_no_connection_held_across_await` | AST 扫描 `src/backend`：`async def` 体内出现 `with connect()` / `with transaction()` 的数量为 **0** | 在某个 async 函数里塞一个 → 红 |
| G-DB5 | `test_pool_exhaustion_times_out` | `size=1, acquire_timeout=0`，借走唯一连接后再借 → **立即**抛 `PoolExhausted` | 去掉超时 → 用例挂住（需 `pytest-timeout` 兜底） |

落点：G-DB1 / G-DB3 / G-DB5 随 T15.1（建池当步交付），G-DB2 随 T15.2，
G-DB4 随 T15.5。**每条守卫都必须与它所守的那一步同批落地**，不留「后面补」。

⚠️ G-DB1 **绝不能**写成「全局 `connect` 总数 == `size`」。两个理由：
① 故障路径 `_replace_locked()` 会补连接，总数必然可能 > `size`；
② 更致命的是它守的是**总数**而不是**时机**——「池空且未满时才建」这种实现最终创建数
仍然恰好是 `size`，改坏了也照绿。**要守的是第 ② 段「并发借用阶段一条都不建」。**

⚠️ G-DB5 的时间依赖必须是 **0**（`acquire_timeout=0`）。本环境**没装 `pytest-timeout`**，
用非零超时写「要能快速失败」会在慢机器上偶发；而反向验证「去掉超时」会**挂死**而不是转红——
挂死会把整个 CI job 拖死，比红更糟。

⚠️ **G-DB4 的优先级要提到 P0，不再是「低成本静态红线」。** 池化把它从"局部报错"变成
"全站不可用"：从前在 `async def` 里跨 `await` 持有连接，最坏是这条连接用不了；
现在则是**长时间占住一条池连接**——4 条池被 4 个协程各占一条，第 5 个请求就 `PoolExhausted`。
一次 LLM 流式可能持续数分钟，这个窗口足够把池锁死。

G-DB1 是原来那把锁的使命继承者：**锁守的是「不要并发引导」，它守的是「引导只发生一次」，后者更强也更可测。**

### 6.3 新增的行为用例

| # | 用例 | 断言 |
| --- | --- | --- |
| 1 | 跨仓库原子性 | 注入「第二个仓库写入失败」，第一个仓库的写入必须**不存在** |
| 2 | 池复用不串味 | 连续 50 次借还，每次写入的行互不可见（除非已提交） |
| 3 | 日志线程与请求线程共用池 | 两者并发写不出现 `database is locked`，也不互相阻塞超过阈值 |
| 4 | `dispose()` 撞上在途借用 | 两个线程各持一条连接并阻塞；`dispose()` 后释放：两线程不抛异常、`in_use` 归 0、后续 `acquire()` 抛 `PoolClosed`（**池不许被复活**） |
| 5 | 启动顺序 | 启动链路里 `init_schema()` 与 `LogRepository.prune()` 借连接时，`stats["size"]` 已等于 `size`（即已 `open()`），不是懒建 |

> 第 4 条防的是"换路径 / teardown 撞上后台日志线程"时的偶发 500；第 5 条防的是
> 「`prune()` 先于 `open()` 跑，于是第一次借用触发了懒建」——这种破坏**只看连接总数发现不了**，
> 必须由启动顺序来守。

### 6.4 测试装置的两处硬要求

1. `conftest._reset_runtime_state()` 里补 `database.reset()`（dispose + 清实例缓存）。
   否则每个用例都会留下一个装着 `size` 条打开连接的池，指向已删除的临时库。
2. `isolated_db` 的 teardown 里加断言：`stats["in_use"] == 0` 且 `idle == size`。
   这样**每一个用例都在守「没有连接被泄漏」**，覆盖面比一条专门用例大三个数量级。

## 7. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| 池复用导致跨请求事务串味 | 归还强制回滚（T15.2）+ G-DB2 守卫；T15.2 必须先于 T15.3 |
| 路径切换时旧连接仍被借出 | `get_database()` 比对路径并 `dispose()`；G-DB3 守卫 |
| 池过小导致请求排队 | `wait_ms_max` 进统计；默认 4（`DB_POOL_SIZE` 可调）；耗尽超时可观测（G-DB5） |
| 仓库批量改签名引入回归 | 分批改 + `conn=None` 缺省；每批跑全量对拍 |
| 改造期系统处于「半池化」 | 允许：`connect()` 委托后，未迁移的仓库照旧走自开事务，语义完全一致 |

回滚方式：逐个提交可独立回退（T15.1–T15.5 各自一个 commit）。**不采用**「一次性大提交」，否则回滚只能整体撤销。
