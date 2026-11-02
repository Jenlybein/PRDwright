# v8 死代码与退役内容清理

> 本文件是 **T18.8** 的执行清单。目标不是"代码好看"，是**仓库里不存在"读不到、也删不掉"的东西**。
> 为什么单列一个阶段：这类内容的成本不在运行时，在读代码的人——每个新人都要先判断
> "这一列/这个组件还能不能用"，判断成本天天发生，而删掉它的成本只发生一次。
>
> ⚠️ 本清单于 2026-11-02 按工作区实测重审过一次：原 §2 的四项（A1~A4）判据仍成立，
> 但**漏了 6 类**，且验收基线写错了（1006）。下方 §2 已补全，§6 已改实测值。

---

## 1 判据

一个东西算"死"，必须同时满足三条，缺一条就不动：

1. **没有任何代码读它**（`grep` 可证，不是"我觉得没人用"）；
2. **删掉它不会让用例变红**（若有，必须在清单里写明为什么仍要删、用例怎么改）；
3. **删掉它不需要改业务逻辑**（需要改逻辑的，属于重构，不属于清理）。

第 3 条是边界：`paste_prd_text` / `draft_prd_text` 这类列看起来"老"，但它们**仍在
`schemas.py` 与 `solution_service.py` 里读写**，不满足第 1 条，因此不在本清单内。

⚠️ **"被文档说是死的"不算证据**：`AppDialog.vue` 被 `v2/ARCHITECTURE.md` 写成
"零外部引用"，实际 `ReviewStage.vue:7` 正在 import 它；照文档删会构建失败。每次删除前必须自己 grep。

---

## 2 已确认清单（可删）

| # | 项 | 证据 | 处置 | 可逆 |
|---|---|---|---|---|
| **A1** | `solutions.data` 列 | `models.py:229`；`solution_repo.py:119` 写空串、`update` 不写；`solution_service` 文件头写「不再写、不再读」 | Alembic `drop_column` + 删模型字段 · **已完成（#8）** | **否** |
| **A2** | `model_providers.builtin` 列 | `models.py:625-627`「v19 起一律为 0，代码不再读取」；全仓零查询 | Alembic `drop_column` + 删模型字段 · **已完成（#9）** | **否** |
| **A3** | `frontend/src/components/ui/AppToast.vue` | 全仓 `grep AppToast` 零命中（含 kebab-case 与全局注册两条逃逸路径） | 删文件 | 是 |
| **A4** | `frontend/src/components/ui/AppEmpty.vue` | 同上，零命中 | 删文件 | 是 |
| **A5** | `frontend/src/components/ui/AppInput.vue` | **原清单漏项**。零 importer；`src` 内唯一命中是 `fieldStyles.ts:9` 的**注释** | 删文件 | 是 |
| **A6** | `services/job_service.py:363 cancel_for_solution` | **原清单漏项**。零调用；`delete_solution_and_cancel_jobs`（`:411-424`）把同样三步内联重写了一遍；3 处注释仍写「删除方案时必须走这里」 | 删函数 + 同步 3 处注释的指向 | 是 |
| **A7** | 10 个零提及的符号 | `normalize_stage`、`is_valid_role`（`core/chat_kinds.py:44/59`）、`master_key_exists`（`core/crypto.py:51`）、`is_present`（`core/secrets.py:119`）、`current_engine`（`core/engine.py:154`）、`config_service_window`、`input_ceiling_for`（`core/runtime_config.py:465/500`）、`ClearForSolution.clear_for_solution`（`chat_session_repo.py:105`）、`ChatTurnRepository.has_turns`（`chat_turn_repo.py:219`）、`_PLACEHOLDER_URL`（`core/schema.py:53`） | 删 | 是 |
| **A8** | 8 条未使用 import（ruff F401 已确认真无消费者） | `api/conversation.py:11`、`api/schemas/`（拆分前为单文件 19/22 行）、`core/engine.py:49/50`、`core/runtime_config.py:54`、`repositories/document_repo.py:34`、`services/conversation_service.py:42` | 删 | 是 |
| **A9** | `backend/src/backend/__init__.py` 的第二个 FastAPI app + `pyproject.toml:36` 坏入口点 | `__init__.py:1-7` 建了 `app` 与 `/health`，全仓 `from backend import app` 零命中；真 app 在 `main.py`、`/health` 在 `api/health.py:144`；`pyproject.toml:36` 的 `backend = "backend:main"` 指向不存在的属性（`hasattr(backend,'main')` 实测 False） | 删 app 代码块；入口点最终**整块删除**（见下方补记） | 是 |
| **A10** | 前端 6 项：3 个孤儿资源 + 2 个零引用函数 + 1 个死类型 | `assets/main.css`（0 importer）、`assets/base.css`（仅被 main.css 引用 → 级联死）、`assets/logo.svg`（0 命中，注意 `grep logo` 会命中 `logout` 是假阳性）、`api.ts:123 getQuestions()`、`api.ts:887 generatePrdStream()`、`types/index.ts:457 SessionData`（**不是** `:1155` 的 `SessionDataV2`，那个活着） | 删 | 是 |

### A9 补记：入口点不是改对值就完事，它有两层坑

层一（本次执行中修掉）：`backend = "backend:main"` 指向包里不存在的属性 —— 只有真按脚本
启动才炸 `AttributeError`，所以长期没人碰。

层二（#4 收口之后才浮出来）：把值改成 `backend.main:app`，属性**确实解析得到**了，票面验收
「指向真实存在的属性」达成；但 console script 是**零参调用**入口对象，而 FastAPI 实例的
`__call__(scope, receive, send)` 要服务器递来的三个参数，于是 `uv run backend` 变成
`TypeError: FastAPI.__call__() missing 3 required positional arguments`。

根因不是「值写错」，是**把 web 应用当成了可执行命令**。应用只由服务器加载
（`uvicorn backend.main:app`，README / 部署 / CI 一直走这条）。最终处置：**把
`[project.scripts]` 整块删除**——全仓零使用者，留着它等于给下一个人（或下一个 agent）
留一个照 `pyproject.toml` 一敲就报错的陷阱；不包 `serve()` 是因为那会让「怎么启动」
多出第二种说法。守卫落在 `tests/test_entrypoint.py`：一条钉住「别再声明 backend 命令」，
一条对**任何**现存命令做 `signature().bind()` 零参可调用性检查（同时覆盖旧坑与新坑）。

### A1 / A2 的删除障碍（必须一起改）

- `models.py:665` 的 `("model_providers","builtin")` 在 `BOOLEAN_COLUMNS` 白名单里，
  该集合被 `migrations/env.py:41,90`、`core/schema_snapshot.py:41,56`、`tests/test_schema_parity.py:23,133` 读。
  **只删字段不删这一行，parity 用例立刻红。**
- `tests/conftest.py:291` 的 `data.pop("builtin", None)` 是给这一列做的兼容垫片，删列后可一并删。
- `tests/test_text_limits.py:109` 手写 `data=""` 构造 `Solution(...)`，删列后这处构造要改。

### 为什么 A1 值得删，而不是"留着当历史备份"

1. **"留着备查"这个用途从未兑现**（无导出功能、无管理端页面读它）；
2. **它会在换库时变成额外决策**（`DIALECT-PORTABILITY.md` §3.3 把它列进 `MEDIUMTEXT` 变体名单，理由是"不知道它会多大"）；
3. **结构对拍要为它买单**（多一列 = `diff_snapshots` 多一条可能的假阳性）。

另有一处**文档自相矛盾**需在删列时一并修：`core/limits.py:6` 仍把 `solutions.data` 当作受
60KB 闸门约束的四列之一，而 `models.py:228` 明写"闸门不覆盖它"——且 `ensure_text_within_limit`
对该列**零调用**。

---

## 3 需人判断（判据 1 成立、2 不成立：只被测试引用，或属公共 API）

**后端 7 项**

| 项 | 证据 | 为什么不能一刀切 |
|---|---|---|
| `document_store.get_document` | 生产零调用（内部走 `_read_document`），22 处测试调用 | 删要连 22 处用例一起改 |
| `review_whole_document` | 生产零调用，docstring 自己写"面向用户的生成链路不要用"；4 处测试调用 | 看着像"给测试留的第二实现" |
| `solution_service.delete_solution` | 生产零调用（路由走 `job_service.delete_solution_and_cancel_jobs`），1 处测试调用 | 同上 |
| `DOWNSTREAM_KINDS` | 零代码引用；`v3/ARCHITECTURE.md:209` 把它当"依赖失效"语义的锚点 | 当文档用还是当数据用 |
| `langchain` 顶层依赖 | `pyproject.toml:13`，backend/src 零 import（只用 `langchain_core`/`langchain_openai`）；`uv.lock` 反向依赖里只有 backend 需要它 | 删前须确认没有隐式 `import langchain` 的插件路径 |
| `python-dotenv` | 零 import，`core/secrets.py:42-48` 明写"刻意不用 `dotenv_values`"；但 `pydantic-settings`/`uvicorn` 依赖它，删声明不会从环境消失 | 收益低 |
| `api/config.py` 空路由 | 0 个端点，被 `main.py:20/240` import，`config.py:10` 挂 TODO | 自述"给将来留的接口" |

**前端 28 项**：全部是"源码零引用、只有 `__tests__` 存活"（`api.ts` 的 8 个流式接口、
`runSubscriptions.detach`、store 的若干导出、`utils/workflow.ts` 的一组守卫函数等）。
其中 `frontend/src/__tests__/AppV2.spec.ts:99-101` 的注释**自己承认**那 8 个流式接口
"应用里已经没有任何调用点"。另：35 处只是 `export` 关键字多余（声明本身活着），删的是 `export` 二字不是代码。

**两类明确不删**：

- `resetForTests`（`runSubscriptions.ts:53`）——测试隔离钩子，属设计如此，判据 1 不适用；
- `pymysql`（`pyproject.toml:24`）——零 import，但 SQLAlchemy 按 `mysql+pymysql` **动态加载驱动**，
  删了 MySQL 部署第一次连接就报 `No module named 'pymysql'`（证据 `tests/test_database_url.py:33,84`）。

---

## 4 文档承诺、但仓库里不存在的路径

这一类不是死代码，是**文档承诺与仓库现实不一致**——更容易误导人：读者照着文档去 `deploy/` 找模板，会以为是自己 clone 错了。

原 §3 只记了 3 份模板；**实测是整个"部署交付物"层从未落盘**，另有两处同类：

| 出处 | 承诺的路径 | 实际 |
|---|---|---|
| `v7/roadmap.md:57,58,59,60`、`v7/ARCHITECTURE.md:846-849`、`v7/README.md:75`、`v8/MULTI-BACKEND.md:262`、`v8/roadmap.md:198` | `deploy/backend.env.example`、`deploy/systemd/prdwright.service`、`deploy/nginx/prdwright.conf`、`scripts/deploy/{backup,restore,release,smoke}.py` | **否**（连 `deploy/` 目录都没有；`scripts/` 下只有 `keygen/generate_keys.py`） |
| `v8/MULTI-BACKEND.md:487,533`、`v8/roadmap.md:246` | `scripts/deploy/test-mysql.sh` | **否** |
| `v8/MULTI-BACKEND.md:485`、`v8/roadmap.md:244` | `core/snapshot/{sqlite,mysql}.py` | **否**（`core/snapshot/` 目录不存在） |
| 已在本轮清理中删除 | `docs/plans/`、`docs/artifacts/` | 目录已删；根 `README.md` 与历史档案索引里的引用已同步改写（历史档案目录随后也整体移除） |

**处置（已定）**：对 `deploy/` 与 `scripts/deploy/` 选「废」——在相关文档标注「未落盘，以 v8 容器化为准」，
不补一套只为存档的裸机模板（v8 之后裸机不是主路径，补了就是两份真相并存）。
`v6/roadmap.md:45-46` 的 `tests/support/`、`tests/fixtures/` 与 `backend/tests/integration/`（早先已被明确否决过的形态）同属此类，一并标注。

> ⚠️ 这与 v5 那批部署文档是同一类问题的**第二次出现**：**文档写了、文件没落盘**。
> 所以 v8 的验收里加一条硬要求：**凡是文档里写出的路径，`ls` 必须能看见**。

---

## 5 重写（阈值：单文件 > 1200 行）

判定口径：**只看文件行数，不看函数行数**——Vue 的 composable 与 Pinia 的 `defineStore` setup 函数
天然等于整个模块（如 `useDocumentPipeline` 1543 行、`useSolutionsStore` 648 行），把它们当"大函数"
等于把"重写整个 store"当成"拆一个函数"。函数口径因此作废。

| # | 文件 | 行数 | 备注 |
|---|---|---|---|
| W1 | `backend/src/backend/services/job_service.py` | 792 | **已完成（#5）**。按职责拆到 `backend/src/backend/services/job_runtime.py` / `job_runtime_events.py` / `job_finalize.py` 等子模块；`_apply_event` 已随迁 |
| W2 | `backend/src/backend/services/prd_section_writer.py` | 1129 | **已完成（#6）**。并行车道拆进新模块 `backend/src/backend/services/prd_section_parallel.py`（474 行） |
| W3 | `backend/src/backend/api/schemas/` | 9 个模块，最大 358 | **已完成（#7）**。无函数，纯 Pydantic 模型，按领域拆模块 |

临界区不入列（`document_service.py` 1144、`prompts.py` 918、`conversation_service.py` 913、
`adminService.ts` 873、`admin.py` 842、`admin_service.py` 835、`admin_config_service.py` 812）。
前端 `useDocumentPipeline.ts` 1627 / `api.ts` 1269 / `types/index.ts` 1201 **本轮不动**，下一轮再议。
拆不动的就不拆——这条优先于"压到 1200 以内"。

**唯一一对 ≥30 行完全重复**：`prd_section_writer.py` 553-585 ↔ 739-771（33 行，
`_stream_linear` 与 `_stream_parallel` 的同一段初始化）。是否抽成私有方法，**留到重写 W2 时一并决定**。

**验收方式**：TDD——先补覆盖再改结构；改前后行为不变由全量用例证明；补的用例只覆盖原本没覆盖的分支；
并给出重写前后的行数/函数数对比。

---

## 6 顺序与不可逆点

```text
笔 1  文档层收敛 + 修正硬错      → 链接自检 + 逐条 ls
笔 2  前端死代码（A3/A4/A5/A10） → npm run build（两个工程）
笔 3  后端死代码（A6~A9）        → uv run pytest + 构建
笔 4  重写 W1~W3（TDD）          → 全量绿 + 行数对比
笔 5  删列 A1/A2                 → 备份 → 迁移 → 结构对拍 → 全量
```

**笔 2/3 先做**：零风险、零依赖，可以立刻验证清理流程本身。

**笔 5 的不可逆性必须说清楚**：

- 删列之后，**回滚代码会让旧代码读不到列**（启动即报错）。所以回滚方式是
  **恢复备份**，不是 `git revert`（同样的推论在删列场景下也成立）。
- 因此三步必须在同一个窗口内完成：`VACUUM INTO` 备份 + 自检 → 跑迁移 → 全量用例。
  备份没做成不许跑迁移。
- SQLite 的 `DROP COLUMN` 需要 batch 模式（`migrations/env.py` 已配 `render_as_batch=True`）。

---

## 7 验收

| # | 判据 | 怎么验 |
|---|---|---|
| 1 | 删掉的列不再出现在任何读写路径 | `grep -rn "\.data\b" backend/src` 只剩注释；`grep builtin` 只剩历史迁移 |
| 2 | 结构对拍全绿 | `uv run pytest tests/test_schema_parity.py` |
| 3 | 全量用例绿 | `uv run pytest`；**工作区基线 1014 例 / HEAD 986 例**（`--collect-only` 口径；`def test_` 文本计数 890，差额来自 parametrize 展开）。原写的 1006 是旧账 |
| 4 | 前端两个工程仍能构建 | `npm run build`（用户端 + 管理端） |
| 5 | 文档里写出的路径都真实存在 | 逐条 `ls` |
| 6 | 库结构里没有"模型没有、库里还在"的表 | `alembic check` 无差异 |

---

## 8 本轮接受的偏差（记录用）

这一节只记录**与既定口径不一致、但有意保留**的地方——不是待办，也不是辩解；写下来是为了
下一轮 review 不必重新判断「这是漏了还是已经决定过」。

- **新增 3 个守卫测试文件 + `test_layering.py` 的棘轮/冻结点**（`backend/tests/test_entrypoint.py`、`backend/tests/test_cleanup_dead_code.py`、`frontend/src/__tests__/dead-code.spec.ts`）：偏离「零新增接缝」，理由是它们钉住的是「删掉的东西不许回流」与「契约形状」，属于仓库既有的**架构守卫**范式，不是新的测试接缝。
- **#2 多删 3 个级联类型**（`GeneratePrdStreamPayload` / `QuestionsConfig` / `QuestionConfig`）、**#3 多删 2 个常量**（`DEFAULT_CHAT_STAGE` / `TURN_ROLES`）：前者的唯一消费者是被删的 `getQuestions` / `generatePrdStream`，后者口径已并入 `backend/src/backend/core/chat_kinds.py` 的 docstring——都是删干净而不是留下「有类型没函数」的空壳。
- **#6 两处行为校准**（失败任务不再被 `done_callback` 摘除 → 恢复「出错只发 error 不发 done」；断开时 `lane.aclose()` 立即取消在途调用）：两处都有新用例覆盖，不是顺手改行为。
- **`prd_section_parallel` 与 `prd_section_writer` 存在双向依赖**（函数内延迟 import），且并行模块会调 writer 的若干私有方法：记录为已知的边界代价——本轮重写只搬不改，拆成两个模块仍比留一个 1129 行的单模块可读。
- **`job_service` 用 `noqa` 再导出 25 个名字（其中 23 个私有名）**：是为保住既有测试的 `import` 面（`js._finalize` / `js._RUNTIMES` 这类直触），与 `api/schemas/__init__.py` 的再导出是同一种范式。

---

## 9 删列的备份窗口（演练记录）

删列不可逆，窗口内三步必须齐：`VACUUM INTO` 备份 + 自检 → 跑迁移 → 全量用例。
A1（`solutions.data`）删除时实际做过一遍自检，记录如下（据 #8 报告）：

1. 临时库先建到 `0002`（删列前的最新结构）；
2. `VACUUM INTO` 生成备份；
3. 三条断言：`PRAGMA integrity_check` = `ok`；备份内 `alembic_version` = `0002`；备份含 `data` 列；
4. 备份 180,224 字节，sha256 `bb1cd19205dc532c163789c8547158d9c84e3e6d9b9e72ccfe1e4b1347b0c20a`；
5. **三条断言通过之后**才执行迁移。

同一窗口内的处置对 #9（`model_providers.builtin`）同样适用。

---

## 附录 A：扫描工具与误报

| 工具 | 命令 | 实测 |
|---|---|---|
| ruff F401/F811 | `uv run ruff check --select F401,F811 backend/src` | 命中 11 条，其中 8 条是真未使用 import（→ A8） |
| vulture | `uv run --with vulture vulture backend/src --min-confidence 80` | ⚠️ **0 条**。该阈值太高；放低到默认 60% 才出 122 条 |
| knip | `npx --yes knip@5 --reporter compact` | 两个工程都跑通；但**不认 vitest 入口**，把 45+14 个 spec 报成"未使用文件"、`@vue/test-utils` 报成"未使用依赖"——**误报** |

**`--min-confidence 80` 这条命令要从清单里改掉**：它给出的是"零命中"的假安全感。放低到 60% 后，
122 条里**真死的只有 1 条**（`DOWNSTREAM_KINDS`），其余 121 条分别是路由函数（19）、
LangChain 覆盖点（3）、Pydantic/SQLModel 字段（74）、测试装置 helper（8）——误报比 **121:1**。

处置规则（照旧）：路由函数、`@field_validator`、模型字段 → **保留**；私有函数零调用 → 删；
公共函数零调用 → 先问"是不是给将来留的接口"，答不上来就删。

## 附录 B：查未引用 `.vue` 的一次性脚本

```python
import pathlib

for base in ("frontend/src", "frontend_admin/src"):
    root = pathlib.Path(base)
    files = [p for p in root.rglob("*") if p.suffix in (".vue", ".ts")]
    texts = {p: p.read_text(encoding="utf-8", errors="ignore") for p in files}
    for p in files:
        if p.suffix != ".vue":
            continue
        used = [q for q in files if q != p and p.stem in texts[q]]
        if not used:
            print("UNUSED", p.relative_to(root))
```

三个注意点（第三个是 A5 被漏掉的原因）：

- 它按**文件名**匹配，所以对"全局注册"（`app.component("AppToast", ...)`）会漏；跑完对每个命中项再 grep 一次确认；
- 对 `backend/` 的模块级分析**不适用**（`from backend.services import x` 这种写法里没有完整模块路径，会误报）；后端请用 `vulture`；
- ⚠️ 它按文件名做**子串**匹配，**任何一句注释里提到名字都会让它漏报**。
  `AppInput.vue` 就是这么被漏掉的（`fieldStyles.ts:9` 的一句注释）。
