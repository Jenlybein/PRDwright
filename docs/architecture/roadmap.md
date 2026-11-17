# PRDwright 现状排期

> **排期真源**：本文件列出 #31~#41 各票的落点与验收方式，以及 #44~#49「入口模式收成一张登记表」这一轮的票序与验收；本轮「审核结论、按结论修复、一键更新与交付页」（spec #51）拆成 **#52~#59 八张票、逐票交付**，票序、落点与实施状态见 §6。架构结论与设计契约见 [`ARCHITECTURE.md`](./ARCHITECTURE.md)（入口模式结论在 §5.8 取舍节，审核结论与修复在 §3.8 / §3.9 / §4.3 / §5.10），阶段规模与界碑见 [`README.md`](./README.md)（时间线真源），架构决策见 [`../adr/0001-architecture-docs-consolidation.md`](../adr/0001-architecture-docs-consolidation.md) 与 [`../adr/0002-review-conclusion-and-fix.md`](../adr/0002-review-conclusion-and-fix.md)。
>
> 由 v9 那一版排期收敛为现状排期：先文档、后代码；其中 T19.0~T19.7 是已交付的历史排期，不再是现状。

---

## 0. 排期总原则

1. **先文档、后代码**：#31 先把 v1~v9 九层压成一版现状文档 + 决策档案，#32~#33 把词表与收口纪律对齐，之后才动代码（#34 起）。
2. **1200 行阈值作废**：文件边界改以**接缝**为依据，取代 v8 附录 A §5 的 W1 口径（见 [`ARCHITECTURE.md`](./ARCHITECTURE.md) §5.4）。七个实现文件归入同一内部包即可，不必合并回单个大文件。
3. **数字先复核再写**：对外动作与名字数量、入口模式同步点数量一律以实测为准，不沿用旧报告里的约数；写文档前 `git grep` 复核。
4. **用例基线现取现算**：`cd backend && uv run pytest --collect-only -q` 取当前收集数；旧文档里的 1089 / 1100 / 1110 / 1127 / 1136 / 1140 / 1149 / 1150 / 1175 及「文本计数 955 / 参数化展开差额 134」整段一起重算，不沿用中间数。#41 终检实测：加入终检守卫前收集数 1175、`def test_` 文本计数 1005、参数化展开差额 170；加入 4 条终检守卫后的终值 1179 / 1009 / 170；#44 再补一条「登记表结论落在指定落点」守卫后为 **1180 / 1010 / 170**；#49 终检复测同值 **1180 / 1010 / 170**（四张代码票只加前端用例）。**#59 收口实测（分支 `ticket/59-closeout`，基于 `7cd85df`）：收集数 1228；文本计数 1051；参数化展开差额 177** —— 文本计数的口径是 `git grep -c 'def test_' -- backend/tests` 逐文件计数求和（本轮与文件系统逐文件读的结果一致，都是 1051），差额 = 1228 − 1051，即一条 `def test_` 被参数化展开成多条收集项后的净增；#52~#58 新增的结论 / 修复 / 重审用例把三个数一起抬了上去。**评审修复轮实测（分支 `fix/spec-51-review`，基于 `1c84255`）：收集数 1234；文本计数 1057；差额 177** —— 新补六条的落点是四条旧通道改收方案（`generate-prd-stream` 与 `generate-prd-from-summary-stream` 各 2 条）、PRD 重审带需求澄清线程（1 条）、审到一半手改即任务失败（1 条）。**第二轮评审修复轮实测（分支 `fix/spec-51-review-2`，基于 `1e58cbc`）：收集数 1238；文本计数 1059；差额 179** —— 新补的是重审第二道闸门失败（三组参数化）、失败时间落库与清空。**#61 收尾实测（分支 `ticket/61-followups`，基于 `main` 的 `bb6a630`）：收集数 1235；文本计数 1057；差额 178** —— 净减 3 的构成：退役四条旧通道删掉 7 条「拿旧路由当接缝」的用例（`test_job_contract`）、鉴权门禁改写后的清单少 4 个入口（`test_auth_gate` 的参数化项），新增 8 条（旧路径 404 ×4 参数化、提示词双上游 1、OpenAPI 反向守卫 1、服务方法保留守卫 1、关闭审核不写失败时间 1）。实测口径见 `docs/agents/conventions.md` §10。以后收口仍按 `--collect-only -q` 现取，别抄这里的数字。
5. **加列不是单向门**：单向门指删列 / 退役列（回滚靠备份库文件）；新增列属于加列，评审不拿迁移成本当架构理由。
6. **一次改动要收口**：代码、架构文档、路线图三者同步，谁都不滞后。
7. **路径即承诺**：文档里写出的每个仓库路径 `ls` 都要能看见，写完逐条 `Test-Path` + `git status` 复核。
8. **提交纪律**：提交与推送前必须经用户确认（先 `git add -An .` 预演）；implementer 只改文件、跑用例、报告，合并由 merger 做。

---

## 1. 票序与依赖

```text
#31 → #32 → #33 → #34 → #35 → #36 → {#37, #38} → #39 → #40 → #41
 #34 另依赖 #33；#35 依赖 #31,#33,#34；#36 依赖 #34,#35
 #39 依赖 #36,#38（不等 #37）；#40 依赖 #37,#38,#39；#41 依赖前十张全部

#44 → #45 → #46 → #47 → #48 → #49   （入口模式收成一张登记表，严格串行）
  #44 文档先行，无前置；#45 依赖 #44；#46 依赖 #45；#47 依赖 #46；#48 依赖 #47；#49 依赖 #48
  四张代码票 #45 / #46 / #47 / #48 + 终检票 #49 全部依赖文档票 #44

#67 → #68 → {#69, #70} → #71   （运行视图跟随在途任务，spec #66）
  #67 文档先行，无前置；#68 依赖 #67；#69 依赖 #68；#70 依赖 #68
  #69 依赖 #68、#70 依赖 #68（两张都吃同一份「按任务种类的在途判据」，不串行会重复一次规则）
  #71 依赖 #68、#69、#70（三张代码票全部完成才收口）

#91 → #92 → #93 → #94   （spec #75 的管线四张，严格串行：后一张吃前一张收口后的形状）
  #91 大纲扩到三份；#92 退役分章（PRD 整篇化）；#93 三份分批续写；#94 统一审核 / 改写 / 修复并删死字段
```

**先文档、后代码**的分界在 #33 / #34 之间：#31~#33 不动生产代码语义，#34 起才改生成任务与数据库投影。第二轮同样先文档、后代码：#44 只把结论写进 [`ARCHITECTURE.md`](./ARCHITECTURE.md) §5.8、本文件、`docs/agents/conventions.md`、`GLOSSARY.md` 与时间线真源 [`README.md`](./README.md)，#45 起才动前端代码，#49 收口三者一致。

---

## 2. 各票落点与验收方式

| 票 | 主题 | 落点 | 验收方式 |
| --- | --- | --- | --- |
| **#31** | 压成一版架构文档 + 新建决策档案 0001 | `docs/architecture/ARCHITECTURE.md`、`docs/architecture/README.md`、`docs/architecture/roadmap.md`、`docs/adr/0001-architecture-docs-consolidation.md`；删九个按层目录；九层之外 8 个文件里的旧层引用逐条改到新落点（另含 `AGENTS.md` / `docs/agents/` / 前端注释等口径，`git diff-tree` 实测 13 个 M 文件） | 正向结构「目标 / 范围（含本期不做）/ 设计 / 契约 / 取舍」齐备；九层摘要表九行；三段承重专题整段可在新文档找到；九层目录已删；`git grep` 无悬空引用；每条写出的路径 `Test-Path` 可见；`git add -An .` 预演；`uv run pytest --collect-only -q` 收集不炸 |
| **#32** | 词表三条定义对齐新形态 | `GLOSSARY.md` 的「架构决策」「层（v1 … v9）」「时间线真源」三条 | 三条与新文档形态一致；「方案」词条仍只指产品概念、不加第二层意思；不新增词条、不收实现概念 |
| **#33** | 收口纪律写进路线图与约定 | `docs/architecture/roadmap.md`、`docs/agents/conventions.md` | 「先文档、后代码」「1200 行阈值作废」「数字先复核再写」「用例基线现取现算」「加列不是单向门」五条写进两份文档；入口模式同步点数量按实测改正 |
| **#34** | DB 行成为任务单不可变数据的完整投影 | `backend/src/backend/core/models.py`、`backend/migrations/versions/`、`backend/src/backend/repositories/generation_job_repo.py` | 建任务那一刻一次写全请求参数 / 配置快照 / 技能包身份；运行态仍按既有节奏合并写；重启不续跑；迁移手写、结构对拍全绿；全流程离线 |
| **#35** | 清理零调用内部名 + 守卫就位 | `backend/src/backend/services/job_service.py` 及同包文件、`backend/tests/test_cleanup_dead_code.py` | 全仓零调用的 10 个内部名删除；`assert not hasattr` 反向守卫语义不破；`git grep` 复核无残留调用；全量用例离线跑绿 |
| **#36** | 扩展——造单 / 登记 / 派发新形状与旧入口并存 | `backend/src/backend/services/` 内部包、`backend/src/backend/api/jobs.py` | 造单是公开动作、不落库不顶替不发事件；登记是一个事务（当场查方案、顶替双标记、落库、写 chat 输入轮次）；两阶段任务单都是不可变对象、派发及之后只认登记后那张；SSE 事件种类与编码、`[DONE]` 一条都不变 |
| **#37** | 迁移批次一——生产调用方 + 排空职责归位 | `backend/src/backend/api/jobs.py`、`backend/src/backend/services/job_drain.py`、`backend/src/backend/services/job_wait.py`、`backend/src/backend/services/job_registration.py`、`backend/src/backend/services/job_ticket.py` | 等候在途任务交回生成任务一侧；排空只问「还有哪些在跑 / 要不要等 / 超时取消哪几支」，不读运行时内部字段；30 秒窗口、摘流、就绪探针、可直接调用形态都不变；启动回收孤儿任务不变 |
| **#38** | 迁移批次二——测试全链路用例改走主接缝 | `backend/tests/test_generation_job.py`、`backend/tests/test_chat_job.py` 等全链路用例 | 断言只跨「造单 → 登记 → 派发 → 订阅到结束」主接缝；假环境在造单时传入（生产真值 / 测试假值）；旧的裸运行时用例替换而非叠加；不新增测试专用私有缝 |
| **#39** | 迁移批次三——登记 / 收尾 / 快照 / 裸运行时用例归位 | `backend/tests/` 中登记、收尾、数据库兜底快照、直接构造运行时四类用例 | 只造单不派发的用例归到登记语义（顶替、目标产物落列）；只派发不登记的用例归到造单 + 派发；直接构造裸运行时的用例全改成跨公开动作断言；方案被删时收尾作终态判定、失败不覆写正文仍成立 |
| **#40** | 收缩——删旧入口、再导出与内部包边界落定 | `backend/src/backend/services/job_service.py`（包入口）、`backend/src/backend/services/job_service_impl.py` 与同包实现文件 | 包入口 `job_service.__all__` 实测只露 9 个正常动作（#37/#38 迁走 `create_job` / `dispatch` 后），新三步 `job_ticket` 与「等待在途」`job_wait` 各有自己的公开面；再导出整块删空、主文件不再当中转；两个仍被生产使用的名字各归其位、调用方从真正所属处引用；排空留在包外；单 worker + 进程内运行时不变 |
| **#41** | 文档 / 路线图 / 词表 / 基线与引用终检 | `docs/architecture/ARCHITECTURE.md`、`docs/architecture/roadmap.md`、`docs/agents/conventions.md`、`backend/pyproject.toml`、`backend/scripts/schema_survey.py`、`backend/tests/test_closeout_docs.py` | 代码、架构文档、路线图、词表与决策档案结论一致；用例基线按收集数现取现算（连文本计数 / 参数化展开差额整段重算）；数字逐条 `git grep` 复核；全仓无悬空引用、文档写出的路径逐条可见；路径扫描白名单按类别写明依据；全量离线跑绿；`git add -An .` 预演并经人确认后才提交 |
| **#44** | 文档先行：登记表结论写进现状架构与路线图 | `docs/architecture/ARCHITECTURE.md` §2.2 / §5.8、`docs/architecture/roadmap.md`、`docs/agents/conventions.md`、`docs/architecture/README.md` §4、`GLOSSARY.md`、`backend/tests/test_closeout_docs.py` | 取舍节写明「一张登记表 / 一行一个入口 / 七类资料为列」与范围（只收随入口变化的事实；14 个视图状态 → 进度格全局映射、后端、界面交互不动）；本文件 §1 / §2 / §5 出现 #44~#49 票序与依赖；约定的人工「约 20 处」清单整段退役并改写实测数字；失真表补齐已复核校正；词表补「未选择」并退役「四处同步」；文档路径逐条可见；不碰前端源码、后端业务与测试夹具 |
| **#45** | 显式「未选择」：收拢入口模式的默认口径 | `frontend/src/utils/workflow.ts`、`frontend/src/stores/workflow.ts`、`frontend/src/stores/solutions.ts` | 「未选择」唯一出口；入口页四卡、老方案恢复并保存、进度条与阶段名按完整流程展示；现有行为断言全绿（含「历史快照无入口模式仍能恢复并保存」） |
| **#46** | 登记表落地：完整流程一行打通全链路 | `frontend/src/utils/workflow.ts`、`frontend/src/types/index.ts`、`frontend/src/AppV2.vue`、`frontend/src/__tests__/AppV2.spec.ts` | 登记表七类列落地、只填「完整流程」一行；落结构化表单 / 六格进度条 / 模式名（store 层，见 [`README.md`](./README.md) §4 第 18 行）/ 返回文案 / 刷新后录入还在 / 三份文档齐才可完成；其余三入口走旧路径、行为不变；用例走外壳集成接缝 |
| **#47** | 其余三个入口迁入登记表、旧表退役 | `frontend/src/utils/workflow.ts`、`frontend/src/stores/solutions.ts`、`frontend/src/composables/useDocumentPipeline.ts` | 四行齐全；快捷流程不追问需求、两个导入入口两格且只要提示词套件即可完成；模式名（store 层，见 [`README.md`](./README.md) §4 第 18 行）/ 阶段名 / 标题回落 / 返回与改需求文案四入口都对；旧表删空前确认零调用，夹具随迁，行为断言全绿 |
| **#48** | 缺行缺列当场失败 + 一条会红的守卫 | `frontend/src/utils/workflow.ts`、`frontend/src/__tests__/workflow.spec.ts` | 未知入口键 / 缺行 / 缺列问询时明确失败、不返回默认值；「未选择」仍合法；假第 5 入口半填即被拒（最高可注入点）；用例只断言外部可观察结果；失败转「该块界面不可用」不白屏 |
| **#49** | 终检收口：代码、架构文档、路线图三者对齐 | `docs/architecture/ARCHITECTURE.md`、`docs/architecture/roadmap.md`、`docs/agents/conventions.md`、`backend/tests/test_closeout_docs.py` | 三者结论一致；用例基线按当前收集数现取现算（含文本计数与参数化展开差额）；路径逐条可见、全仓无悬空引用；`git add -An .` 预演确认无 `.env` / `.venv` / `node_modules` / `dist` |

---

## 3. 最终形状（#34~#40 的共同目标）

```text
造单（公开动作、不落库 / 不顶替 / 不发事件 / 不做方案校验，允许读技能包清单）
  原始需求 + ①事件流来源 + ④配置 / 供应商快照 + ③技能包身份（算好的数据）
  → 单①：无 id、不可变
登记（一个事务：当场查 ②方案还在不在、顶替旧任务双标记、落库一次写全、写 chat 输入轮次）
  → 单②：带 id 与四个 chat 派生字段、不可变（新对象）
派发 / 订阅 / 收尾 / 排空：只认单②
```

数据库行 = 单里**不可变数据**的完整投影；运行态以内存为权威、按既有节奏落进度；重启仍中断在途任务。「方案是否仍存在」是登记与收尾的当场查询，不冻结在单上；「技能包身份」是数据不是能力。

**一条都不变**：SSE 事件种类与编码、`[DONE]`、单 worker + 进程内运行时、重连先快照后 live、失败不覆写正文、被取代与已取消分开、排空顺序与 30 秒、就绪探针摘流、启动回收孤儿任务、前端交互、错误码。

---

## 4. 本期不做

- 不改任何产品能力：三份文档、澄清、审核、改写、技能包内容、鉴权与配额、管理端与用户端交互都不变。
- 不引入多 worker、外置运行时表或跨进程事件总线；不做在途任务续跑或回放。
- 不重开已关闭的双后端系列；唯一部署后端是 **Postgres**（v9 起 #78 已收缩到单一后端：SQLite / MySQL 分支删除、迁移链只剩一份 PG 基线、旧 SQLite 库拒绝启动）。
- 不在仓库里落规格文件或临时工单目录；规格与票住在 GitHub Issues。

---

## 5. 验收总表（#41 收口时逐条打勾）

| # | 判据 | 怎么验 |
| --- | --- | --- |
| 1 | 架构结论只有一版现状文档 | `ls docs/architecture` 只有 `README.md` / `ARCHITECTURE.md` / `roadmap.md` |
| 2 | 决策档案从 0001 重新编号 | `ls docs/adr`：0001 是文档重组，0002 是本轮「审核结论与修复」 |
| 3 | 全仓不再有指向九层目录的悬空引用 | `git grep -n -E 'v[1-9]/(ARCHITECTURE\|README\|roadmap)\.md'` 无命中 |
| 4 | 文档写出的每个路径都能看见 | 一次性脚本扫反引号里的仓库路径，逐条 `Test-Path` |
| 5 | 三段承重专题整段在现状文档里 | 按附录 A / B / C 标题定位核对 |
| 6 | 全量用例离线跑绿、零 LLM Key | `cd backend && uv run pytest`；基线按 `--collect-only -q` 现取现算 |
| 7 | 代码、架构文档、路线图三者同步 | 逐票对照 §2 的落点表 |
| 8 | 提交前预演并经人确认 | `git add -An .`，确认无 `.env` / `.venv` / `node_modules` / `dist` |
| 9 | #44~#49 票序与依赖写在现状排期 | 对照本文 §1 的第二段票序、§2 的六行落点与 §5 本表；#44 文档先行，#45 / #46 / #47 / #48 四张代码票与 #49 终检票严格串行 |
| 10 | 登记表结论与全局映射边界只按现状架构文档作答 | `docs/architecture/ARCHITECTURE.md` §5.8 写明一张登记表、一行一个入口、七类资料为列；14 个视图状态 → 进度格的全局映射、后端、界面交互不动 |

---

## 6. 本轮：审核结论、按结论修复、一键更新与交付页（#52~#59 + #61，逐票交付）

本轮按 spec #51 拆成 **#52~#59 八张票 + #61 收尾票、逐票交付**：先文档（[`../adr/0002-review-conclusion-and-fix.md`](../adr/0002-review-conclusion-and-fix.md)、本文、[`ARCHITECTURE.md`](./ARCHITECTURE.md) §3.8 / §3.9 / §4.3 / §5.10、[`GLOSSARY.md`](../../GLOSSARY.md)、[`../agents/conventions.md`](../agents/conventions.md)），再代码（数据 → 契约 → 管线 → 前端），最后由 #59 把三者核对到一致、#61 收掉 spec 留下的三条尾巴（两个错误码进枚举、生成链路审核失败落库、四条旧直连路由退役）。到 `7cd85df` 为止前七张票已并入 `integration/spec-51`；#59 在 `ticket/59-closeout` 上做，#61 在 `ticket/61-followups` 上做（基于 `main` 的 `bb6a630`）。**合并与发布仍由人确认；发布（先备份 → 换代码 → 重建 → 线上验证）本轮未执行**。

### 6.1 范围

- **审核结论入库**：随**文档版本**保存、一版一条；只在审阅页显示当前版本那一条；**审核失败不写结论**（界面标「审核失败」），但**失败时间随版本落库**（`review_failed_at`，迁移 0007）：刷新后仍显示「审核失败（时间）」，拿到新结论时清空。
- **两个新任务动作**：按结论**修复**（重写并**追加版本**，同一任务内只再审一次、不再改写）与**重新审核**（只写结论、不碰正文、不追加版本）。
- **输入一律服务端取**：库内产物取当前版本正文，导入入口的材料取方案上保存的录入文本，PRD 的需求输入（结构化摘要、自由文本、需求澄清历史）同理由服务端取；**请求体不再携带上游正文与需求正文**；四条旧的非任务通道当时同步改收方案，随后在 #61 **删除**（见下方「收尾三条尾巴」）。
- **黄条「一键更新」**：用当前上游重新生成被点的那一份（先确认，写明旧版本保留在版本历史）；不新增任务种类。
- **交付页**：去掉「完成」按钮，**齐备即可进入**（判据由入口模式决定）；进度条「交付」格在齐备后可点；三份齐备时全应用提示一次（每方案每会话一次），**不自动跳转**。
- **顺手修正**：PRD 串行链路补发汇总结论；`rewritten` 如实汇总。
- **收尾三条尾巴**（#61）：`REVIEW_TARGET_MISSING` / `CONTENT_CHANGED` 进错误码枚举（前后端镜像 + 调用点改用枚举）；生成链路的审核失败也落 `review_failed_at`（与重审失败同一列、同一展示，显式关闭审核不写）；四条旧直连生成通道退役（路由 + 请求模型 + 前端封装），用例接缝迁到任务通道。

### 6.2 本期不做

审核结论的历史列表 UI；按正文变化判定结论过期；结论进交付包或管理端统计；审核通过后自动改写；交付页自动跳转（理由见 [`ARCHITECTURE.md`](./ARCHITECTURE.md) §2.2 与 §5.10）。

### 6.3 票序、落点与实施状态

| 票 | 阶段 | 落点 | 状态 |
| --- | --- | --- | --- |
| **#52** | 数据 + 管线 | `document_versions.review` 列与迁移；PRD 串行链路补发汇总结论、`rewritten` 如实汇总 | 已合入 `integration/spec-51`（`d4127e4`） |
| **#53** | 契约 | 生成与修复的输入由服务端解析（`job_inputs`），建任务那一刻解析、缺材料 400 | 已合入（`7447f00`） |
| **#54** | 契约 | 请求体删上游与需求字段；两个旧的非任务通道改收 `solution_id`；字段白名单与固定投影用例同步 | 已合入（`7d50278`） |
| **#55** | 管线 + 前端 | 重新审核（只审不改、覆盖当前版本结论）+ 结论面板四态 | 已合入（`d6eb14b`） |
| **#56** | 管线 | 修复：重写整篇 → 只重审一次（不改写）→ 追加版本、正文与结论同批写入 | 已合入（`0e0ac53`） |
| **#57** | 前端 | 过期黄条「一键更新」：确认后用当前上游重生成被点的那一份 | 已合入（`a5aa763`） |
| **#58** | 前端 | 交付页去「完成」、进度条「交付」格按齐备解锁、齐备提示每方案每会话一次 | 已合入（`8c617cd`） |
| **#59** | 收口 | 三份文档与实现一致（§3.8 / §3.9 / §4.3 / §5.10 与约定 §5 逐条核对）；基线现取现算；全量离线跑绿 | 本轮（`ticket/59-closeout`，基于 `7cd85df`） |
| **第二轮评审修复** | 收口 + 管线 + 前端 | 硬违规 3 条（版本来源标签 / `DocKind` 清单 / `goTo` 注释）；条件写第二道闸门也判失败并向订阅者补 `error`；**US3 审核失败落库**（迁移 0007 + 读侧 `DocumentItem` / `DocumentVersionDetail` + 前端 `reviewSnapshots`）；坏味 4 条（产物上下文结构、需求文本折叠收成一处、`doc_kind` 校验文案一处出、`stream_review_only` 参数改名）；测试接缝（面板行为断言迁到应用外壳、后端新用例改外部可观察行为） | 本轮（`fix/spec-51-review-2`，基于 `1e58cbc`） |
| **#61** | 收尾（错误码 + 管线 + 契约清理 + 文档） | 两个错误码进 `ErrorCode` 与前端 `BACKEND_ERROR_CODES` 镜像、调用点改用枚举；生成时的审核失败写 `review_failed_at`（`review` 事件带显式 `attempted`，运行态照着记）；四条旧直连生成路由与请求模型退役、用例接缝迁到任务通道、OpenAPI 反向守卫；§2.2 / §3.8 / §5.10 与约定 §5 同步 | 本轮（`ticket/61-followups`，基于 `main` 的 `bb6a630`） |

### 6.4 验收与状态

1. 刷新 / 换设备后，审阅页显示的仍是同一条审核结论 —— **已成**：结论随版本读回，SSE 只做订阅期即时展示。
2. 修复后新版本带新结论；修复任务内的重审**不触发改写** —— **已成**。
3. 重新审核不改正文、不追加版本 —— **已成**。
4. 任务请求体里不再有上游正文与需求正文字段；导入入口材料未落库时返回明确 400 —— **已成**。
5. 一键更新使用当前上游；旧版本在版本历史里可查 —— **已成**。
6. 三份齐备即可进入交付页；「完成」按钮不存在；提示每方案每会话一次 —— **已成**。
7. `cd backend && uv run pytest`、两个前端的 `test:unit` 与构建全绿（全流程离线、零 LLM Key）—— **跑绿**：#59 实测后端 **1228 passed**；`frontend` 的 `test:unit -- --run` **735 passed / 39 文件**、`type-check` 与 `build` 通过；`frontend_admin` 的 `test:unit -- --run` **117 passed / 13 文件**、`type-check` 与 `build` 通过（管理端本轮无改动，只做回归）。用例基线按 `--collect-only -q` 现取：**1228 收集 / 1051 文本计数 / 177 差额**（口径见 §0 第 4 条与 [`../agents/conventions.md`](../agents/conventions.md) §10）。**评审修复轮复测**：后端 **1234 passed**；`frontend` 的 `test:unit -- --run` **740 passed / 39 文件**、`type-check` 通过；基线 **1234 收集 / 1057 文本计数 / 177 差额**。**第二轮评审修复轮复测**：后端 **1238 passed**；`frontend` 的 `test:unit -- --run` **745 passed / 40 文件**、`type-check` 通过（单独跑，不与后端并发）；基线 **1238 收集 / 1059 文本计数 / 179 差额**。**#61 收尾复测**：后端 **1235 passed**；`frontend` 的 `test:unit -- --run` **746 passed / 40 文件**、`type-check` 与 `build` 通过；`frontend_admin` 的 `test:unit -- --run` **117 passed / 13 文件**、`type-check` 通过（管理端零改动，只做回归）；基线 **1235 收集 / 1057 文本计数 / 178 差额**。
8. 两个新错误码在前后端码表里逐项一致、调用点写枚举而不是字面量 —— **已成**：`ErrorCode.REVIEW_TARGET_MISSING` / `CONTENT_CHANGED`，跨端一致性由 `backend/tests/test_error_contract.py` 锁死。
9. 生成时审核失败 → 该版本 `review_failed_at` 非空，刷新 / 换会话后仍显示「审核失败」；显式关闭审核的入口不写、仍「未审核」 —— **已成**：`backend/tests/test_review_persistence.py` 的两条用例分别守住「失败落库」与「没审不落库」。
10. 四条旧直连路由已删除、OpenAPI 里不再出现、无调用方残留 —— **已成**：`backend/tests/test_cleanup_dead_code.py`（OpenAPI 反向守卫 + 服务方法保留守卫）、`backend/tests/test_job_contract.py`（旧路径 404 ×4）。
11. 发布前备份、发布后就绪探针与线上三点验证（刷新后结论仍在 / 修复追加版本 / 交付页没有「完成」按钮）—— **未执行**：本轮不部署、不 push、不合并进 `integration`，由编排方与用户确认后做。

---

## 7. 本轮：审核只报缺陷（ADR 0003）

线上一次真实审核给出 17 条「问题」，大半是「未定义 / 依据不足 / 未登记清单」这类**未决**，与待确认清单重叠；而审核**读不到清单**却报出了「未登记到清单」。按「**缺陷 vs 未决**」重切分工：审核只报可验证的错处，未决归清单。决策与否决理由见 [`../adr/0003-review-defects-only.md`](../adr/0003-review-defects-only.md)，现状落点见 [`ARCHITECTURE.md`](./ARCHITECTURE.md) §3.8 与 §4.3（C-R5）。

### 7.1 范围

- **六类缺陷**：编造 / 矛盾 / 结构不符 / 空泛 / 推断未标注 / 清单漏登记；结论**不回答**「缺什么、该怎么定」。
- 每条问题带**类别**；类别固定严重度（编造、矛盾 = 高；其余 = 中），不让模型升降。
- **两个时点两套口径**：生成时只判前五类；重审与修复后的重审才判「清单漏登记」，并注入当前版本的待确认清单。
- 「推断未标注」**只判形式**（有没有「（建议）」与一句依据），不评依据强弱。
- 修复提示词写死**不得为缺失编造**：缺就写「待确认」并登记清单。
- 面板标题改「**文档缺陷**」，折叠头分类计数、展开按类分组；旧结论没有类别 → 归「未分类」，不追溯。

### 7.2 本期不做

旧结论重分类；跨版本的缺陷历史；把未决并回审核结论。

### 7.3 落地顺序

文档（ADR 0003、§3.8、§4.3 C-R5、约定、词表）→ 提示词与解析（类别 + 清单注入 + 两时点）→ 契约（结论 JSON、读面、前端类型）→ 面板分组与改名 → 固定夹具四条断言 → 收口。

### 7.4 验收

1. 正文有推断且已标「（建议）」、清单有对应项 → 重审**不报**。
2. 正文有推断、清单没有 → 重审报「清单漏登记」（带章节与缺的条目意思）。
3. 生成时同一份正文 → **不报**漏登记（清单尚未生成）。
4. 编造事实 → 高；结构 / 空泛 / 未标注 → 中，且中档不触发自动改写。
5. 修复提示词含「不得编造」约束；夹具断言修复后「缺」处仍是待确认，而不是被填成一个像样的默认值。
6. 面板标题为「文档缺陷」、按类分组；旧结论显示「未分类」。
7. 三份产物用同一套类别集；后端与两个前端的用例、构建全绿（零 LLM Key）。

### 7.5 落地与复测（`ticket/63-review-defects-impl`，基于 `integration/spec-63` 的 `6c6e4bc`）

- **代码落点**：唯一口径模块 `backend/src/backend/services/review_defects.py`（类别→固定严重度、别名归一、两时点开关、清单注入文本、旧形状归一、`issues_text` 与 `defects_count_text`、条目被丢光后的 `finalize_verdict`）；`document_review`（接口文档 / 提示词整篇）与 `prd_section_writer`（PRD 逐章 + 重审；票 #92 起 PRD 也走 `document_review` 整篇链路，`prd_section_writer` 已退役）共用同一套提示词与解析，提示词只有一份、靠 `{pending_section}` 占位符区分生成与重审两态；`job_inputs` 在建任务那一刻取当前版本的待确认清单（重审 / 修复各一处），生成链路不带。前端：`ReviewConclusionPanel` 标题改「文档缺陷」、折叠头分类计数、展开按类分组（类内 high 在前、未分类垫底），`types/index.ts::ReviewIssue.category` 与 `utils/review.ts::parseReview` 归一，标签与后端 `CATEGORY_LABELS` 逐项逐序一致（有跨端对拍用例）。
- **七条验收各有用例**（`backend/tests/test_review_defects.py` **23 个用例函数 / 25 条收集项**，覆盖七条验收 + PRD 空清单仍开第六类 + 别名与旧形状分路；B1 / B2 / B3 与 C1 / C2 的收口各有守卫；前端 `ReviewConclusionPanel.spec.ts` / `review.spec.ts` 守标题、计数、分组、未分类与解析归一）。
- **复测**（前后端分开跑，全流程离线、零 LLM Key）：后端 `uv run pytest` **1260 passed**；`frontend` 的 `test:unit -- --run` **752 passed / 41 文件**、`type-check` 通过；`frontend_admin` 的 `test:unit -- --run` **117 passed / 13 文件**（管理端零改动，只做回归）。用例基线按 `--collect-only -q` 现取：**1260 收集 / 1080 文本计数 / 180 差额**（口径见 [`../agents/conventions.md`](../agents/conventions.md) §10）。
- **未执行**：push 与合并（由用户确认）；发布与线上三点验证不在本轮。


---

## 8. 本轮：运行视图跟随在途任务（spec #66，#67~#71 逐票交付）

本轮把「这一份文档正在被写」这件事从「只由界面相位决定」改成「由**在途任务**兜底」。结论写在 [`ARCHITECTURE.md`](./ARCHITECTURE.md) §5.11，两个新词定义在 [`GLOSSARY.md`](../../GLOSSARY.md)；#67 只动文档与守卫，不碰产品代码。

票序见 §1：**#67 → #68 → {#69, #70} → #71**。

| 票 | 主题 | 落点 | 验收方式 |
| --- | --- | --- | --- |
| **#67** | 文档先行：词表定「在途任务 / 运行视图」，架构文档落三处取舍 | `GLOSSARY.md`、`ARCHITECTURE.md` §5.11（及 §2.2 / §3.8 的两处相左口径）、`roadmap.md` §1 / 本节；守卫 `test_closeout_docs.py` | 词表两条各自只表达一半，界面上不出现一词指两事；取舍三处都写清「选了什么 / 放弃什么 / 何时改回来」；票序与本节齐备；写出的路径逐条可见 |
| **#68** | 运行视图跟随在途任务：切回来还在修复页，带「查看当前版本」出口 | 前端应用外壳的渲染判据、压制位、收尾落点 | 修复中切走再切回被修文档 → 屏幕上仍是运行视图；点「查看当前版本」停在审阅页、离开再回来重新落回；切到别的产物不被打断；收尾不抢页面。断言只跨应用外壳接缝 |
| **#69** | 修复在途时按任务种类禁用三份产物的修复 / 重审 / 保存编辑 | 外壳把「按任务种类的在途判据」传给审阅页与结论面板；面板禁用态 | 三份入口全禁用、不附说明文字、不弹提示；收尾后恢复可点；原有「发起时静默 return」保留为最后一道 |
| **#70** | 刷新兜底归位：按方案查在途任务，修掉把产物种类当任务种类的传参 | 应用外壳的刷新归位兜底、任务列表读接口的调用参数 | 指针在 → 回运行视图并按快照重锚；指针丢 → 仍按方案查到在途任务并归位重连；请求不再把产物种类当任务种类传；每次刷新只触发一次、不轮询；后端零改动 |
| **#71** | 收口：文档与实现对齐、用例基线现取现算、整栈验证 | `ARCHITECTURE.md`、`roadmap.md`、`conventions.md`、前端用例 | 代码、架构文档、路线图逐条核对无矛盾；基线按收集数现取现算并写回本节；用户端用例、`type-check`、`build` 全绿（零 LLM Key） |

**先文档、后代码**的分界在 #67 / #68 之间：#67 只把结论写进词表、现状架构文档与本文，不碰产品代码；#68 起才动前端渲染。**本期不做**见 [`ARCHITECTURE.md`](./ARCHITECTURE.md) §5.11 的「范围」与 §2.2 新增的那一行（进度条运行态、侧栏角标、顶部横幅、跨标签页同步、跨进程重启接续）。

### 8.1 落地与复测（`integration/spec-66`，基于 `main` 的 `4f98776`）

- **代码落点**（#68 / #69 / #70，全部在前端外壳与三个组件内，后端零改动）：运行视图的渲染判据、压制位与「查看当前版本」出口在应用外壳（`frontend/src/AppV2.vue`，取舍见 [`ARCHITECTURE.md`](./ARCHITECTURE.md) §5.11）；「查看当前版本」这个链接本身在运行视图组件；入口禁用落在结论面板与审阅页（面板只看「这一份允不允许修」，`fixInFlight` 只看「有没有一支修复在途」——两件事两种禁用，别混成一条）；刷新归位的兜底在应用外壳，按方案 + 在跑状态查一次、再按后端 `doc_kind` 归位。
- **两条判据分开写**（这是本轮最容易写错的一处）：`在途任务` → 禁用三份产物的入口（粒度是**任务种类**，与产物无关，因为后端顶替判据不含产物）；`在途任务 + 产物对得上 + 未被压制` → 渲染运行视图（粒度才是产物）。把这两条合成一条，会得到「修 PRD 时接口文档的审阅页也变成运行视图」。
- **复测**（前后端分开跑，全流程离线、零 LLM Key）：后端 `uv run pytest` **1261 passed**；`frontend` 的 `test:unit -- --run` **766 passed / 41 文件**、`type-check` 与 `build` 通过。用例基线按 `--collect-only -q` 现取：**1261 收集 / 1081 文本计数 / 180 差额**（口径见 [`../agents/conventions.md`](../agents/conventions.md) §10；本轮后端只多了一条文档守卫，前端多十三条：**#68** 切视图回运行视图 / 压制位与出口 / 收尾不抢页 / 收尾时人就在这份文档上不指错路 / 重审在途也接管 / 一键更新在途也接管，**#69** 三份入口禁用 / 收尾后恢复，**#70** 指针在时归位 / 指针丢了仍归位 / 停在别的产物也归位 / 多支并行时优先接修复 / 兜底只查一次，另加一条「接管清单覆盖每处 `job.start`」的守卫）。
- **新增用例的接缝**：全部落在**应用外壳集成接缝**（真 router + 点按钮 / 点进度条 + 断言屏幕上渲染的是哪张页），判据是「审阅页独有的『文档缺陷』面板在不在」；不读组件私有状态。少数用例另外断言了 `viewState` 的取值——那不是判据，而是**旁证**：为了证明「相位确实已被覆写成审阅态、屏幕却仍是运行视图」这件事真的发生了，否则这条用例对「视图跟随运行态」什么都证明不了。
- **后端只加了一条文档守卫**（`test_closeout_docs.py` 里的同文件新函数，不新建测试文件、不新增业务用例），基线因此 1260 → 1261；这与 spec #66「后端零改动」的口径一致——没有动任何后端接口、迁移或任务状态模型。
- **未执行**：push、开 PR、合并与线上验证（合并与推送由用户确认）。

---

## 9. 本轮：状态这件事重做（spec #75，#79~#85 逐票交付）

spec 见 [`state-flow-design.md`](./state-flow-design.md)：把任务状态从「三份真源各说各话」改成「数据库是唯一裁判 + 显式状态机 + 终态与落稿同事务 + 统一版本锁」。本轮按票拆开交付，先立状态机与账本，再收原子收尾与版本锁，租约 / 恢复 / 七态 / 部分完成随后。现状落点见 [`ARCHITECTURE.md`](./ARCHITECTURE.md) §3.2、§3.3。

### 9.1 范围（本票 #81）

- **原子收尾**：成功收尾的五件事（宣布成功 / 记一条账 / 写正文新版本 / 写审核结论 / 清租约占位）收进 `_finalize` 的一个事务；任一步失败整批回滚，任务保持 `running`，由恢复机制接手。
- **统一版本锁**：所有正文写入走唯一入口 `document_store.write_document(mode, expected_version_no)`；版本号对不上返回机器判据 `version_conflict`，不覆盖用户手改 / 新生成的正文。
- **写正文失败不再谎报成功**：删掉「异常吞成 warning 后照报 completed」的旧口径。
- **前端**：手改撞版本锁时明确提示「正文已被更新，请刷新后重试」并停止覆盖重试；保存失败不显示成功。

### 9.2 本期不做

#81 的「本期不做」：七态与「取消中」（#84）、租约与周期恢复器（#82 / #83）、事件表与快照（#86）、部分完成落成新版本（#85）、排队 / 抢占 / 退避（#88）。前端不新增界面，只加冲突提示与失败不显示成功。其中 #82 / #83 / #85 / #84 / #88 已在后续票落地（见 §9.3）。

### 9.3 票序与状态

| 票 | 主题 | 落点 | 状态 |
| --- | --- | --- | --- |
| **#79** | 状态机单一入口（三态先行） | `services/job_state.py`：四事件 + 期望条件的唯一转换动作；仓储终态写条件化（`WHERE status='running'`）、影响行数为判据；`_finalize` 幂等闸 | 已合入 `integration/spec-75`（`9b7f421`） |
| **#80** | 执行账本 | `job_attempts` + `repositories/job_attempt_repo.py`；与状态更新同一事务、保存点隔离（审计写坏不杀结论） | 已合入（`cf25ec1`） |
| **#81** | 原子收尾 + 统一版本锁 | `services/job_finalize.py` 单事务收尾；`services/document_store.py::write_document` 唯一写入入口 + `expected_version_no`；`repositories/document_repo.py::write_current_version` 条件写；`PUT .../documents/{kind}` 409 + `CONTENT_CHANGED`；就地动作的 `expected_version_no` 随任务单定格；前端冲突提示与「保存失败不显示成功」 | 本轮（`ticket/81-atomic-closeout`，基于 `cf25ec1`） |
| **#82** | 抢占 + 租约 + 动作点续租 | `0003` 三列（`lease_owner` / `lease_token` / `lease_expire_at`）、`services/job_claimer.py` 抢占 / 续租、状态机 `CLAIM` 事件、收尾三条件回写 | 已合入（`d7b89bc`） |
| **#85** | 部分完成落成新版本 | `0004` 两列（`document_versions.partial` / `generation_jobs.partial`）、失败收尾追加部分完成版本（审核两列留空、与任务标记同事务）、前端三处提示 | 已合入（`cf2b2a0`） |
| **#83** | 恢复器（过期租约收口） | `services/job_recovery.py`：`recover_once(limit)` 可直接调用（不起线程 / 定时器）；扫描时旧凭证 fencing（凭证 + 到期时间两列进 WHERE）；周期档按账本剩余次数回排队 / 判失败（`LEASE_EXPIRED`），启动档一律落终态；`reap_orphans` 收编为 `recover_once(startup=True)` | 已合入（`4f668ff`） |
| **#86** | 事件表与游标订阅 | `job_events` 表（迁移 `0005`）+ `services/job_events.py`；`GET /api/jobs/{id}/stream` 按游标读表、断线重连不重不漏；快照防冷读回放；清理是可调用单元 | 已合入（`8727973`） |
| **#88** | 失败延迟退避 | `0006` 一列（`generation_jobs.next_run_at`）、`services/job_backoff.py` 唯一映射表（限流 / 5xx / 超时）、`job_state.transition(REQUEUE)` 同批写退避、仓储到期闸门 `_due_now` 与排队顺序 `_queue_order`、前端等待倒计时与退避期取消 | 本轮（`ticket/88-backoff`，基于 `b3a7940`） |
| **#84** | 七态与「取消中」 | `job_state` 七态转换表（`REQUEST_CANCEL` / `CONFIRM_CANCEL` / `EXPIRE_CANCELLING` / `EXPIRE_PENDING` / `SUPERSEDE` / `CONFIRM_SUPERSEDED`）；仓储条件写按七态重写（`pending` 是唯一「排队中」）；`cancelling` 经动作点续租 → `CancelRequestedError` → 确认取消；恢复器兜底 `cancelling` + 启动档收 `pending`；`0007` 旧行一次性映射；前端 / 管理端七态文案与「正在取消」按钮 | 本轮（`ticket/84-seven-states`，基于 `integration/spec-75` tip） |
| **#89** | 管理端四个数字 | `repositories/generation_job_repo.py::stuck_condition`（卡死判据唯一一处）+ `admin_repo.py::job_metrics`（排队 / 成功率 / 平均耗时 / 卡死）；`GET /api/admin/stats` 扩字段、`GET /api/admin/jobs?stuck=true`；管理端概览加一块四数字（卡死数可点）、任务页加「卡死」标签 | 本轮（`ticket/89-metrics`，基于 `be5be18`） |

### 9.4 验收（#81）

1. 注入「写正文」失败：任务不进入已定稿、正文与版本都不变，账本不落 —— `test_atomic_closeout.py：写正文失败两条`（库行仍 `running`、旧版本原样、无结束事件）。
2. 版本号对不上：写入被拒绝、不覆盖 —— 服务层返回 `version_conflict`；HTTP `PUT` 回 409 + `CONTENT_CHANGED` 且正文不动；就地改写的任务判失败且正文保持新生成那一版。
3. 五条写入路径走同一入口 —— 手改 / 整篇生成 / 修复 / 恢复 / 就地改写各打一次 `write_document`（`mode` 分别为 `manual` / `append` / `replace` / `append` / `append`）。
4. 账本、正文、审核结论属于同一版本 —— 成功收尾后三处同源；重审结论「写进去之后抛错」时状态 / 账本 / 结论一起回滚（任务改判失败、账本只有失败那一行）。
5. 失败整批回滚、任务仍执行中、恢复接手 —— 回滚后启动回收（`reap_orphans`）把它判死并记账；租约恢复是 #82。
6. 账本与硬事务的取舍 —— 正文 / 状态是权威（硬事务），账本是审计（保存点）；账本写坏只丢一行账，成功收尾照常提交。
7. 前端：手改撞版本锁提示「正文已被更新，请刷新后重试」且不重试覆盖；保存失败时「存为新版本 / 恢复」不报成功。

### 9.5 复测（`ticket/81-atomic-closeout`，基于 `cf25ec1`）

- 后端 `uv run pytest`（`-n auto`，单独跑、不与其它全量并发）：**1368 passed / 0 skipped / 0 failed**；用例基线按 `--collect-only -q` 现取：**1368 收集 / 1164 文本计数 / 204 差额**（口径见 [`../agents/conventions.md`](../agents/conventions.md) §10）。
- `frontend` 的 `test:unit -- --run`：**773 passed / 41 文件**；`npm run build`（含 `type-check`）通过。
- **未执行**：push、合并与发布（由用户确认）。

### 9.6 范围、验收与复测（#85 部分完成）

**范围**：失败 / 取消收尾把已生成的正文落成一版带标记的正文，用户可下载、可修、可重生成。

- **落库**（迁移 `0004`）：`document_versions.partial` 与 `generation_jobs.partial`，都是 `BOOLEAN NOT NULL DEFAULT false`。标记的真源在**版本行**（「哪一份正文不完整」），任务行留一份镜像（「这次有没有东西能用」）。
- **写入**：失败收尾经唯一入口 `document_store.write_document(mode="append")` **追加**一版并标 `partial=true`，同一事务里 `review` / `review_failed_at` 显式留空；任务行标记与终态同一条语句。一个字都没写出来、被取代、以及就地改写 / 重审 / 对话的失败都不落部分完成版本（理由见 [`ARCHITECTURE.md`](./ARCHITECTURE.md) §3.3）。
- **前端**：审阅页页首提示「这份正文不完整，已生成的部分可以先用」、版本列表 / 预览标「部分完成」、审核区显示「未审核」且不渲染旧结论、下载与修复不被标记禁用；失败收尾若已落这一版，落点从运行视图改到审阅页。

**验收（逐条都有对应用例，`backend/tests/test_partial_completion.py`）**：

1. 重试耗尽 / 中途失败产生一版带标记正文 —— `test_exhausted_retries_land_a_partial_version`、`test_completed_but_partial_task_marks_the_version_it_wrote`。
2. 那一版审核结论与审核失败时间均为空 —— `test_partial_version_carries_no_review_and_no_review_failed_at`。
3. 任务失败但看得出「有内容可用」 —— `test_job_row_marks_partial_while_status_stays_failed`、`test_jobs_api_exposes_the_partial_marker`、`test_version_list_exposes_the_partial_marker`。
4. 下载与修复对该版本可用 —— `test_documents_api_serves_the_partial_version_for_download`、`test_fix_path_accepts_the_partial_version`。
5. 标记挂版本上、任务上留一份且同一事务 —— `test_marker_is_on_the_version_and_mirrored_on_the_job`、`test_partial_version_and_job_marker_come_from_one_closeout`。
6. 用户取消也落部分完成版本 —— `test_user_cancel_also_lands_a_partial_version`；负例（没有内容 / 被取代 / 重审）各有断言。

**复测**（`ticket/85-partial`，基于 `7b9c2ad`）：后端 `uv run pytest -n auto`（单独跑）**1401 passed / 0 skipped / 0 failed**（收集 1401）；`frontend npm run test:unit -- --run` **789 passed / 41 文件**；`npm run build`（含 `type-check`）通过。**未执行**：push、合并与发布（由用户确认）。

### 9.7 范围、验收与复测（#83 恢复器）

**范围**：过期租约的收口。执行者的进程没了之后，那一行不再永远停在「执行中」。

- **入口**：`services/job_recovery.py::recover_once(limit) -> RecoveryReport` 是**可直接调用的单元**——不起线程、不起定时器、不 sleep（接入调度循环是 #88）。`scan` / `recover_one` 也在公开面：AC3 靠它们把「扫描之后、收口之前」的窗口固定在两步之间。
- **判权**：扫描只挑候选，改不改得到由状态机的条件写回答；`WHERE` 里是**扫描时读到的旧凭证**（`lease_token` + `lease_expire_at` 两列一起）。凭证被换掉、或到期时间被续租推后都改到 0 行——跳过、不报错、不记账（丢弃日志与其它转换同一口径）。
- **去向**：账本行数 = 已结束的执行次数（本票不加迁移，库里没有 `attempt_count` 列）；`attempts + 1 >= DEFAULT_MAX_ATTEMPTS` 判失败（`failed + LEASE_EXPIRED`），否则回排队（清租约；#84 之后落回 `pending`）。**启动档一律落终态**（有租约按 `LEASE_EXPIRED`、从没被领过的排队行按 `INTERNAL_ERROR` +「服务重启，任务已中断」）：那一刻没有执行者会来领，而本票还没有调度循环，回排队等于永远进行中。
- **账本**：每次交接记一条超时账（`error_code=LEASE_EXPIRED`、摘要写明「执行者进程已消失、租约到期未续」），与状态更新同事务（#80 口径）；执行者不在场，用量与耗时记 0（那是未知，不是没花）。
- **收编**：`reap_orphans` 改为 `recover_once(startup=True)`，候选集不看到期时间、也含「登记后没派发」的残留行；不再是一次性批量判死。
- **前端零改动**：原因文案随 `error_message` 由 DB 快照展示；运行视图收到终态后回审阅页并说明「任务已中断」是 #84 的活。

**验收（逐条都有对应用例，`backend/tests/test_job_recovery.py`）**：

1. 杀掉执行中的进程后任务离开执行中 —— `test_a_dead_executors_job_leaves_executing_and_is_claimable_again`、`test_used_up_attempts_turn_the_job_failed_with_lease_expired`、`test_recovery_is_a_noop_when_nothing_has_expired`。
2. 恢复时记一条超时账本、写明超时原因 —— `test_recovery_records_the_timeout_with_a_stable_reason`、`test_each_recovery_round_appends_exactly_one_ledger_row`。
3. 凭证被换掉的行不被误改（改到 0 行即跳过、不报错） —— `test_a_replaced_token_is_skipped_instead_of_updated`（扫描之后另一执行者抢占换了 token）、`test_a_renewed_lease_is_not_stolen`（凭证不变、只把到期时间续到后面）。
4. 重启后不存在「永远进行中」的任务 —— `test_startup_recovery_settles_every_job_that_was_executing`、`test_startup_recovery_reports_the_restart_for_a_row_never_claimed`。
5. 恢复器自身守状态机规则 —— `test_recovery_settles_a_cancelling_row_as_cancelled_not_failed`、`test_a_row_cancelled_mid_window_is_settled_as_cancelled_not_overwritten`、`test_recovery_module_only_writes_state_through_the_state_machine`（AST：必须调 `transition`，不许调 `finish` / `fail` / 取消系列 / 旧的 `reap_running`）。

**复测**（`ticket/83-recovery`，基于 `7b9c2ad`；已并入 `integration/spec-75` 的 #85 之后）：后端 `uv run pytest -n auto`（单独跑、不与其它全量并发）**1413 passed / 0 skipped / 0 failed**（收集 1413 = 合并前基线 1401 + 本票 12 条）。第一次全量有 1 条 DB 连接类假红（`test_second_login_revokes_the_first_session` 撞 psycopg `OperationalError`），单跑绿、第二次全量全绿，按「先单跑、再看错误类型、再重跑全量」的口径记为环境串扰，不记回归。**未执行**：push、合并与发布（由用户确认）。


### 9.8 范围、验收与复测（#88 失败延迟退避）

**范围**：可重试失败**回排队之前先等一段时间**，按错误类型区分，避免连续限流在几十秒内把重试次数耗光。本票只把「到期」这个判据落库并用例化——**调度循环当时仍然不存在**（原计划挂在 #89；#89 实际只做了管理端指标）。→ 后续已接上，见 §13。

- **迁移 `0006`**：`generation_jobs.next_run_at`，`VARCHAR(32) NOT NULL DEFAULT ''`，`down_revision = "0005"`。空串 = **立即到期**（不是「没有值」），于是到期判据只有一种形状。
- **错误码 → 退避秒数的唯一映射表**：`backend/src/backend/services/job_backoff.py` 的 `BACKOFF_SECONDS`——限流（`INVALID_REQUEST`，仓库里 HTTP 429 的稳定码）**30 秒**、上游 5xx（`LLM_UPSTREAM_ERROR`）**10 秒**、超时（`LEASE_EXPIRED`）**5 秒**、其余兜底 5 秒。按 **code 值**分类，不看中文文案 / `detail`；调用方一个数字都不写。
- **写入点只有一处**：`job_state.transition(REQUEUE, …)` 把「退避 + 清租约 + `updated_at` + 账本」压进同一条条件 UPDATE、同一个事务（不存在「已回排队但还能被立刻领走」的窗口，也不存在收尾之外的第二条写路径）。
- **到期闸门与排队顺序各只有一处定义**（`repositories/generation_job_repo.py` 的 `_due_now` / `_queue_order`）：

  ```sql
  -- 抢占 / 候选扫描共同的条件（字面即 SQL，两条入口同一处定义）
  WHERE status = 'running'
    AND (lease_token = '' OR lease_expire_at < now)
    AND (next_run_at = '' OR next_run_at <= now)     -- 到期闸门（#88）

  -- 闸门之后的排队顺序（唯一一处 `_queue_order`）
  ORDER BY updated_at, id
  ```

  规则固定为：**先看是否到期（闸门在 WHERE 里），到期之后才按 `updated_at` 排队**。回排队把 `updated_at` 推到现在 = 排到同优先级队尾；`created_at` 不再参与排序。两套语义各管一段，不叠加成第三种顺序。
- **时间入口可注入**：`transition(now_text=…)` / `claim(now=…)` / `recover_once(now=…)` 都接受注入的时刻（唯一时钟入口仍是 `core/clock.py`），用例毫秒级演示 30 / 10 / 5 秒。
- **前端**（`frontend/src`）：等待态**不显示成失败、也不无限转圈**——运行视图换成「等待重试，约 xx 秒后」（到点转「即将重试」，向上取整 + clamp 到 0，不跳负数、不闪烁），进度条与转圈动画在等待态不渲染；**退避期间取消仍然可用**（等待面板上的「停止生成」调 `POST /api/jobs/{id}/cancel`，等待态任务没有运行时，所以取消后前端自己收尾本地运行态并回到审阅页）。数据来源是 DB 兜底快照新增的 `next_run_at` 字段。

**验收（逐条都有对应用例，`backend/tests/test_job_backoff.py`；前端在 `frontend/src/__tests__/jobBackoff.spec.ts`）**：

1. 限流退 30 秒 / 上游 5xx 退 10 秒 / 超时退 5 秒，映射集中一处 —— `test_backoff_table_is_the_single_mapping`、`test_backoff_keys_are_all_stable_codes`、`test_unknown_code_falls_back_to_the_shortest_wait`、`test_requeue_presses_the_backoff_of_its_failure_code`（三个参数化用例）、`test_backoff_time_is_a_pure_function_of_the_injected_clock`。
2. 未到期的任务不被抢占 —— `test_a_backoff_row_stays_unclaimed_until_its_time_comes`（真实链路：抢占 → 租约过期 → 恢复器回排队 → 到期前领不到、到期后领得到）、`test_the_queue_scan_also_skips_rows_that_are_not_due_yet`（扫描侧同口径）。
3. 排序规则「先看到期，到期后按更新时间」 —— `test_queue_order_is_due_gate_then_updated_at`（未到期不是候选、到期后按 `updated_at`、`created_at`/id 不参与）、`test_a_requeued_row_goes_to_the_tail_of_the_due_queue`。
4. 同一任务连续失败的时间间隔符合退避设置 —— `test_two_consecutive_failures_each_wait_the_full_backoff`（两次失败各等满 5 秒、两次失败之间隔着一整轮租约）、`test_a_rate_limited_task_waits_thirty_seconds_between_retries`（限流那一路等满 30 秒）。
5. 时间入口可注入、用例不靠 sleep —— `test_the_due_judgement_follows_the_injected_moment_not_the_wall_clock`（判据只看注入时刻与库里的列，与真实流逝无关）+ 全文件无 `sleep`。
6. 周边：退避期间取消仍即时生效 —— `test_cancelling_a_waiting_job_still_works_immediately`；快照带等待信息 —— `test_the_db_snapshot_tells_the_frontend_when_the_retry_comes`、`test_a_job_that_never_failed_carries_an_empty_next_run_at`。
7. 前端：等待原因 / 不显示成失败 / 倒计时不跳负数 / 退避期取消仍可用 —— `jobBackoff.spec.ts` 的 9 条（工具 4 条 + `GeneratingStage` 等待态 5 条）。

**复测**（`ticket/88-backoff`，基于 `integration/spec-75` tip `b3a7940`）：后端 `uv run pytest -n auto`（单独跑、不与其它全量并发）**1445 passed / 0 skipped / 0 failed**（= 合并前基线 1428 + 本票 17 条）。前端 `npm run test:unit -- --run` **805 passed / 42 文件**；`npm run build`（含 `type-check`）通过。**未执行**：push、合并与发布（由用户确认）。

**已知遗留（给后续票）**：

- **调度循环**（原计划挂在 #89，当时未做）：到期之后由谁来扫、多久扫一次、扫到怎么派发——本票不起后台定时器，退避只把判据落库。→ 后续已接上（§13），回排队的行会被领取周期领走。
- **「可重试失败」转换**（#84）：本票能真实产生的是超时那一路（恢复器回排队带 `LEASE_EXPIRED`）；限流 / 上游 5xx 两档要等 #84 把「执行中 → 排队中」这条转换接上，映射与写入路径已全部定死，接线时不需要动退避代码。
- **等待中的订阅**：等待态任务没有运行时，DB 兜底快照发完就结束（不是本票改的语义）。等调度循环接上之后，「等服务端事件」这一步才有意义；在那之前靠快照里的 `next_run_at` 显示倒计时。



### 9.9 范围、验收与复测（#84 七态 + 取消中）

**范围**：把任务状态从「三态 + 两个状态性错误码」扩成**七个值**，并给「取消」补上中间态
`cancelling`。一次改透后端、用户端、管理端，**不留兼容映射层**。

- **七个值**：`pending` 排队中 / `running` 执行中 / `cancelling` 取消中 / `succeeded` 已定稿 /
  `failed` 失败 / `superseded` 被取代 / `cancelled` 已取消。对外 `status` 只给这七值；
  `completed`、`CANCELLED`、`SUPERSEDED` 一起退役（`error_code` 只解释失败原因）。
- **转换表只有一份**（`backend/src/backend/services/job_state.py` 模块 docstring）：
  `CLAIM`（pending→running）/ `SUCCEED`（running→succeeded）/ `FAIL`（running→failed）/
  `REQUEST_CANCEL`（pending→cancelled、running→cancelling，执行中那支**不清租约**）/
  `CONFIRM_CANCEL`（running/cancelling→cancelled，清租约并补一次载荷）/
  `EXPIRE_CANCELLING`（cancelling+旧凭证→cancelled，恢复器兜底）/
  `EXPIRE_PENDING`（启动档 pending→failed）/
  `SUPERSEDE`（在途三态→superseded，清租约）/ `CONFIRM_SUPERSEDED`（补一次载荷、不改状态）/
  `REQUEUE`（running→pending，写退避与账本）/ `EXPIRE`（running+旧凭证→failed）。**所有状态写都经它**。
- **「排队中」只有一个表示**：建行即 `pending`；`claim` 只认 `pending`；回排队写回 `pending`。
  不存在「`running` + 空租约」这种第二真源；启动档也按 `pending` 收口（否则永远排队中）。
- **执行者的取消感知**：动作点续租改到 0 行时回读那一行——`cancelling` 抛
  `CancelRequestedError`（与 `LeaseLostError` 分开），协程跑到下一个动作点后确认取消落 `cancelled`。
- **迁移 `0007`**（`down_revision = "0006"`）：**不改结构**，只做一次幂等旧行映射——
  `failed+CANCELLED`→`cancelled`、`failed+SUPERSEDED`→`superseded`（错误码清空）、
  `running` 且无租约→`pending`、`pending` 带租约→清租约。可重复跑，空库 0 行；
  `downgrade()` 有意为空（数据映射不可逆，回滚用备份）。
- **前端**（`frontend/src/utils/jobState.ts` 是唯一一份七态文案/档位表）：运行视图徽标新增
  「排队中 / 取消中」；点停止后按钮**立刻**变「正在取消…」并置灰（不等网络往返，终态由
  订阅链收口）；`cancelled` / `superseded` 收尾落到审阅页并说清「不是失败」；
  查询在途任务统一传 `pending,running,cancelling`（`ACTIVE_JOB_STATE_QUERY`）。
- **管理端**（`frontend_admin/src/views/JobsView.vue` / `DashboardView.vue`）：任务列表按七值
  显示，`cancelling` 行给禁用按钮「取消中…」；「在飞」标签查三值；日统计把
  `succeeded_count` / `failed_count` / `cancelled_count` / `superseded_count` / `active_count`
  分开显示，失败率只算 `failed/(succeeded+failed)`。管理员的取消是最终裁决（直接确认取消落
  `cancelled`），不留等人确认的中间态。

**验收（逐条都有对应用例，`backend/tests/test_job_seven_states.py`；前端
`frontend/src/__tests__/jobSevenStates.spec.ts`；管理端 `jobs.spec.ts` / `dashboard.spec.ts`）**：

1. 排队中取消立刻 `cancelled`；执行中取消先 `cancelling` 且租约不动 ——
   `test_cancelling_a_queued_job_lands_cancelled_immediately`、
   `test_cancelling_a_running_job_enters_cancelling_and_keeps_the_lease`、
   `test_second_cancel_request_on_a_cancelling_job_stays_cancelling`。
2. 执行者在下一个动作点看到取消、退出并落 `cancelled` ——
   `test_executor_confirms_the_cancel_at_the_next_action_point`、
   `test_renewal_on_a_cancelling_job_is_a_cancel_signal_not_a_lease_loss`。
3. 恢复兜底：`cancelling` + 过期租约收成 `cancelled`；启动档同样收；活租约不碰 ——
   `test_recovery_settles_a_stuck_cancelling_job_as_cancelled`、
   `test_recovery_does_not_touch_a_cancelling_job_whose_lease_is_still_alive`、
   `test_startup_recovery_cancels_a_cancelling_row_instead_of_failing_it`、
   `test_startup_recovery_settles_a_queued_row_as_interrupted`。
4. 七值契约与错误码退役：外部 `status` 七值、`error_code` 不再背状态语义、前后台分支同步 ——
   `test_status_values_are_exactly_the_seven_and_partitioned`、
   `test_superseded_and_cancelled_are_two_distinct_states_with_empty_codes`、
   `test_terminal_transitions_clear_all_three_lease_columns`、
   `test_job_list_filter_accepts_the_active_status_set`、
   `test_admin_job_list_status_filter_accepts_the_active_set`、
   `test_admin_state_branches_do_not_guess_status_from_error_codes`、
   `test_state_machine_module_has_no_status_carrying_error_codes`；前端
   `jobSevenStates.spec.ts` 的七态文案 / 档位、`cancelling` 按钮、运行态取消标记；
   管理端七值标签与两数分开。
5. 被取代与已取消始终是两件事，成功统计排除两者 ——
   `test_superseded_and_cancelled_are_two_distinct_states_with_empty_codes`、
   `test_admin_daily_stats_separate_cancelled_from_superseded`（失败率分母不含两者）；
   管理端 `dashboard.spec.ts` 的「已取消 / 被取代 / 失败率分母」用例。

**复测**（`ticket/84-seven-states`，基于 `integration/spec-75` tip）：后端
`uv run pytest -n auto`（单独跑、不与其它全量并发）**1474 passed / 0 skipped / 0 failed**；
用户端 `npm run test:unit -- --run` **813 passed / 43 文件**、`npm run build`（含
`type-check`）通过；管理端 `npm run test:unit -- --run` **120 passed / 13 文件**、
`npm run build`（含 `type-check`）通过。**未执行**：push、合并与发布（由用户确认）。

**给后续票的遗留**：

- **调度循环**（原计划挂在 #89，当时未做）：`pending` 现在有唯一的表示，但「谁来领、多久扫一次」当时仍不存在（→ 已在 §13 接上）；
  启动档只能把上次残留的 `pending` 判死（`EXPIRE_PENDING`），不能重新派发。
- **真实在途任务的管理端取消**：管理员的 `_force_cancel` 直接确认取消（最终裁决）；
  若同一进程里确实有协程在跑，它会在下一个动作点发现自己失去写权而停止（协作式，不是强杀）。
- **回排队与账本**：`REQUEUE` 现在落 `pending`；首次真实「执行中 → 排队中」的可重试生产者
  仍要等调度循环接上之后才有（#88 的映射已就绪；循环已在 §13 接上）。
- **旧行映射只做一次**：`0007` 只在升级时执行；如果存量库里有 `running` + 空租约的行，
  升级后立刻变成 `pending`，不会被启动回收误判成「重启中断」。

### 9.10 范围、验收与复测（#89 管理端四个数字）

**范围**：管理端统计里能看到**四个数字**——排队数、成功率、平均耗时、卡死数——用来提前发现
「重试把任务打成失败」这类恶化。**不新建独立看板**（在既有 `DashboardView` 上加一块）；
**不新增迁移**（纯查询与展示）。

- **四个数字由后端一次查全**：`repositories/admin_repo.py::AdminStatsRepository.job_metrics()`
  返回 frozen dataclass `JobMetrics`（不返回裸 dict），挂在既有 `GET /api/admin/stats` 上
  （新增字段 `queue_count` / `success_rate` / `avg_duration_seconds` / `stuck_count` /
  `stuck_threshold_minutes`）。前端只做格式换算。
- **口径**（无时间窗：`queue_count` / `stuck_count` 是当前快照，
  `success_rate` / `avg_duration_seconds` 是累计）：
  - 排队数 = `count(status='pending')`，**含退避等待中（`next_run_at` 未到期）的行**；
  - 成功率 = `succeeded / (succeeded + failed)`，**分母不含 `superseded` / `cancelled`**，
    分母为 0 时 `null`（不是 0%）；
  - 平均耗时 = 已收尾（成功 + 真失败）`avg(epoch(updated_at) - epoch(created_at))`；取消 / 被取代不进；
  - 卡死数 = `stuck_condition` 的行数：`status='running'` 且（`lease_expire_at` 非空且早于现在
    **或** `updated_at` 早于 `now - STUCK_MINUTES`）。「仍 running 但租约已过期」是 #83 恢复器
    收口之前的真实窗口，必须计入；判据只有一处（`generation_job_repo.stuck_condition`），
    `GET /api/admin/jobs?stuck=true` 与 `is_stuck` 列都用它，卡片数字与点进去的列表因此一致。
- **管理端**：`DashboardView.vue` 加「四个数字」一块（各带一句口径说明；成功率旁写明
  「排除被取代与已取消」；卡死数可点，落 `/jobs?stuck=1`）；`JobsView.vue` 加「卡死」标签
  （`stuck=true` 服务端筛选）；`adminService.ts` 扩类型与 `stuck` 查询参数；耗时格式化抽到
  `src/format.ts` 供概览与列表共用。

**验收（逐条都有对应用例）**：

1. 四个数字由数据库查询得出 —— `test_four_numbers_are_computed_by_the_server_from_the_database`、
   `test_empty_platform_reports_null_rate_and_duration`；管理端 `dashboard.spec.ts`
   「四个数字都在界面上…」。
2. 成功率排除被取代与已取消 —— `test_success_rate_excludes_superseded_and_cancelled`、
   `test_success_rate_is_null_when_only_cancelled_and_superseded`；界面口径文案
   「排除被取代与已取消」。
3. 卡死只由服务端判定 —— `test_stuck_count_includes_a_running_row_whose_lease_has_expired`
   （租约过期窗口 + 卡片与列表同源）、`test_stuck_count_ignores_finished_rows_with_old_timestamps`；
   管理端 `jobs.spec.ts`「从概览的卡死数点进来…」「切到卡死标签…」。
4. 界面显示四个数字 —— `dashboard.spec.ts` 的四数字 / 成功率 `null` 显示「—」两条。
5. 不新建独立看板 —— 未新增路由或视图，四数字落在既有 `DashboardView` 的一块里。

**复测**（`ticket/89-metrics`，基于 `integration/spec-75` tip `be5be18`）：后端
`uv run pytest -n auto`（单独跑、不与其它全量并发）**1483 passed / 0 skipped / 0 failed**
（= 合并前基线 1474 + 本票 9 条）；管理端 `npm run test:unit -- --run` **125 passed / 13 文件**
（基线 120 + 5 条）、`npm run build`（含 `type-check`）通过。**未执行**：push、合并与发布
（由用户确认）。

**给后续票的遗留**：

- **调度循环**：原计划挂在 #89，但本票只做指标——「谁来领、多久扫一次」当时仍未立票（→ 已在 §13 接上）；卡死数
  正是用来观察那段没有调度时的积压。
- **看板趋势图**：四数字只给当前 / 累计快照，没有逐日曲线；趋势仍靠既有「按日趋势」的
  失败率 / 生成数 / Token 三条柱。
- **成功率的敏感度**：累计口径在数据量大之后变化很慢；要看出「今天开始的恶化」得看按日
  趋势，或以后给四个数字加时间窗（那会改接口参数，属于新票）。

### 9.11 范围、验收与复测（#91 大纲扩到三份）

**范围**：三份产物（PRD / 接口文档 / 提示词）在写正文之前都先出一份**大纲**，三份走同一条
阶段顺序「大纲 → 撰写 → 审核 → 改写」。大纲**只从上游材料抽取**、不发明；抽不出的结构标
「待确认」；抽不出可追溯结构时**自动降级为直接写正文**，链路不卡死。**不新增迁移**（大纲
是运行时过程信息，沿用 `stage_text` / `stage_chunk` 口径、不落库）。

- **三份来源各不相同、但都只读上游**（`backend/src/backend/services/doc_outline.py`，
  纯字符串、零 LLM 调用）：PRD ← 需求输入（结构化摘要按字段路径归类）+ 技能包模板章节骨架；
  接口文档 ← PRD 的接口清单（功能编号 / HTTP 方法 + 路径 / `/api/…` 条目优先入「接口列表」）；
  提示词 ← PRD 的功能与场景 + 接口文档的接口条目。**下游不从自己的产物取**。
- **「只抽取不发明」两道机制，不靠一句提示词**：① 结构上根本没有创作这一步——大纲的每条
  条目都是上游原文里的**整行**（锚点逐字保留，显示才截断）；② 抽出后 `verify_outline`
  后校验，锚点在上游里查不到出处就作废、该节降级为「待确认」，因此即便将来别的路径塞条目
  进来也过不了闸。骨架也不另立一套：接口文档的五节与提示词的四节分别必须在
  `API_DOCS_GENERATION_PROMPT` 与 `prompts-guide.md` 里能找到（用例守着）。
- **降级路径**：上游空白 / 骨架缺失 / 一条锚点都抽不到 / 抽取异常 → `build_outline` 返回
  `None`，`outline_events` 只发一条说明性的 `stage(outlining)`（文案写明「直接开始撰写正文」），
  随后照常 `stage(writing)`；**不发 `error`**，任务照常成功，前端阶段条不报错。
- **事件形状不变**：`stage` 多一个取值 `outlining`、大纲正文走既有 `stage_chunk`（只进阶段
  输出面板）；大纲不进 `done.reply`、不进 `chunk`、不参与交付齐备判据（仍按入口模式）。
- **前端**：`DocPhase` 加 `outlining`；`DocPhaseProgress.vue` 加「大纲」一格（不复核的快捷
  流程为「大纲 → 生成 → 完成」）；`StageOutputStream.vue` 把大纲单独一块（`outlineText`，
  正文撰写期间仍显示）；`useDocumentPipeline.ts` 把 `stage_chunk(outlining)` 累计到大纲槽、
  并给 PRD 路径补上 `onStageChunk`；`stores/runs.ts` 预览态加 `outlineText`；`AppV2.vue` /
  `GeneratingStage.vue` 接线。

**验收（逐条都有对应用例）**：

1. 三份产物都能看到大纲阶段 —— 后端 `test_api_docs_stream_announces_outline_then_writing`、
   `test_prompts_stream_announces_outline_then_writing`、
   `test_prd_stream_shows_outline_before_the_first_section`；前端 `DocPhaseProgress.spec.ts`
   「默认五格…」、`AppV2.spec.ts` 的四条大纲用例。
2. 抽不出的结构标「待确认」、不编造 —— `test_outline_marks_unextractable_sections_as_pending`、
   `test_verify_outline_drops_fabricated_anchors`、`test_prd_outline_follows_the_template_sections`、
   `test_api_docs_outline_takes_structures_from_the_prd`、
   `test_api_docs_outline_structure_matches_the_generation_prompt`、
   `test_prompts_outline_structure_matches_the_skill_guide`。
3. 大纲失败仍能继续写正文 —— `test_blank_upstream_degrades_to_plain_writing`、
   `test_degrade_is_reported_honestly_in_the_stage_note`、`test_prompts_stream_degrades_too`；
   前端 `AppV2.spec.ts`「大纲降级：直接进入撰写，阶段条不报错」、
   `DocPhaseProgress.spec.ts`「大纲降级后直接进撰写…」。
4. 大纲内容可在阶段输出里看到 —— `test_outline_stage_carries_doc_type`；前端
   `StageOutputStream.spec.ts` 的三条大纲用例与 `AppV2.spec.ts`「大纲出现在阶段条上…」。
5. 大纲不影响交付判据 —— `test_outline_never_leaks_into_the_deliverable_body`（大纲不进
   `done.reply` / `pending_items`）；齐备判据本身未改，仍由登记表 `requiredDocs` 决定。

**复测**（`ticket/91-outline`，基于 `integration/spec-75` tip `f74e1bf`）：后端
`uv run pytest -n auto`（单独跑、不与其它全量并发）**1500 passed / 0 skipped / 0 failed**
（= 合并前基线 1483 + 本票 17 条）；前端 `npm run test:unit -- --run` **822 passed / 43 文件**
（基线 813 / 43 + 9 条）、`npm run build`（含 `type-check`）通过。**未执行**：push、合并与发布
（由用户确认）。

**给后续票的遗留**：

- **大纲不落库**：它是过程信息（同 `stage_text`），刷新后看不到；要「回看某次生成定了哪些
  结构」得给它一列或一张表——那会改 schema，留给 #92~#94 决定。
- **本地化预测偏差**：结构骨架与关键词表是**保守**的启发式（宁可标「待确认」也不猜），
  上游措辞与关键词表对不上时会多出几个待确认节；等三份产物真正收敛成同一套模板后再收窄。
- **只会「抽取」不会「补全」**：源材料缺的章节永远标待确认；「要不要补写、什么时候补」
  属于后续澄清链路，不在本票。

---

## 10. 本轮：PRD 整篇化 + 分章退役（#92）

### 10.1 范围

- **一个生成路径**：PRD 与接口文档 / 提示词共用同一条整篇生成路径——一次模型调用写完整篇（`generate_prd_stream` → 一条 `stage(writing)` → 连续 `chunk` → `done.reply`），一次整篇审核（`document_review.stream_review_only`，上游注入**原始需求 / 需求澄清对话**），有问题再整篇改写一次（`fix_document_stream` + `part{replace}` 整段覆盖，改写后不重审）。PRD 的 `chunk` 不再带 `section_id` / `index`，前述事件形状与另两份产物**逐字段一致**。
- **退役分章实现**：删除 `services/prd_section_writer.py`（1503 行）与 `services/prd_section_parallel.py`（464 行）及四个 PRD 专用分章提示词；删除五个 PRD 专用配置键（`prd_pipeline_concurrency` / `prd_section_max_retries` / `prd_section_review_enabled` / `prd_section_max_rewrites` / `prd_section_review_max_tokens`）与后台「分章节生成」配置组；`job_runtime` / `job_runtime_events` / `job_snapshot` / `job_finalize` / `job_recovery` / `job_state` 里的 `_SectionLive` / `live_by_index` / `parts_by_index` / `completed_sections` 相关分支一并删除（`completed_sections` 列保留、不再写，留给 #94 收口）。
- **前端退役多章看板**：删除 `components/stages/PrdProgress.vue`；`GeneratingStage` 不再有 `prd` 分支与 `draftOnly`（三份产物共用同一套 `DocPhaseProgress`）；`useDocumentPipeline` 删除看板装配 / 快照还原 / 三段预览，PRD 与另两份走同一套 handlers（`onChunk` 只带 `{text}`，`part{replace}` 整段覆盖）；`types` / `api` / `stores/runs` 的章节级字段（`PrdBoardState` / `PrdSectionCard` / `CompletedSection` / `SectionDraft` / `liveBySection` / `completedSections`）删除。
- **审核与修复同口径**：PRD 审核从「逐章审 + 汇总」改为「整篇审一次」；`review_document_stream` 不再有 PRD 分支，`_rereview_after_fix` 不再走章节写入；失败章节清单字段保留在契约里、当前恒为空（给 #94）。

### 10.2 本期不做

- 三份产物的**产物语义**统一（章节模板、审核口径细节、上游注入字段）归 #93；`failed_sections` / `partial` 等兼容字段的最终去留归 #94。本票只统一**生成路径与事件形状**，不顺手改另两份产物的内容语义。
- 分章的历史版本不做数据迁移：`generation_jobs.completed_sections` 列继续存在（旧值只读、不再写）。

### 10.3 验收与证据

1. **PRD 不再分章并行**：`backend/tests/test_prd_whole_stream.py`（8 条）守「撰写一次调用、无章节归属、整篇审一次、高严重度整篇改写一次且不重审、审核看到完整需求、两条整篇入口（重审 / 修复）、SSE 端到端」；`test_review_defects.py` / `test_review_job.py` / `test_review_persistence.py` 的 PRD 用例全部改成整篇口径。
2. **前端一套进度**：`PrdProgress.vue` 与 `PrdProgress.spec.ts` 删除；`AppV2.spec.ts` 新增「PRD 生成显示共用阶段条与大纲内容」一条，旧的「分章节阶段展示」整块（多章车道 / 按章装配 / 改写叠字等）随看板退役删除。
3. **分章代码与用例退役**：后端收集数从 **1500** 降到 **1443**（删掉三个分章测试文件（52 条收集项）与散布在各文件的分章断言（13 条收集项），新增 `test_prd_whole_stream.py` 8 条）；前端 `test:unit -- --run` 从 **822 passed / 43 文件** 降到 **751 passed / 42 文件**（删 71 条：`AppV2.spec` 的「分章节阶段展示」整块 66 条 + `PrdProgress.spec` 5 条，`api.spec` 的章节字段用例 2 条改为 1 条，另新增 1 条共用阶段展示用例）。
4. **PRD 仍然写出并落一版**（最高优先级回归）：`test_sse_end_to_end_prd_generation_lands_a_version` 走「提交 → 大纲 → 撰写 → 审核 → 定稿 → 落版本」，断言 `document_store.list_versions` 得到 `version_no 1` 且正文等于 `done.reply`。
5. **进度信息不空转**：阶段条（大纲 → 撰写 → 审核 → 改写 → 完成）+ 阶段输出在 PRD 生成期间照常显示，前端用例守着「大纲内容真的出现」；不再有「看板空着 = 生成中什么都看不到」。

### 10.4 复测与收口

- 后端：`cd backend && PRDWRIGHT_TEST_POSTGRES_URL=…:5434 uv run pytest -n auto -q` → **1443 passed，0 skipped，0 failed**（#91 收口基线为 **1500 passed**；本票净减 57：删 65 条收集项、新增 8）。`--collect-only -q` 现取 **1443** 收集 / **1246** 文本计数 / **197** 参数化展开差额。
- 前端：`cd frontend && npm run test:unit -- --run` → **751 passed / 42 文件**；`npm run build`（含 `type-check`）通过。
- 文档：`GLOSSARY.md`（「分章」降级为文档内部结构的**章节**叙述）、本文件 §10、[`ARCHITECTURE.md`](./ARCHITECTURE.md) §3.3（新增「PRD 整篇化」条）、[`../agents/conventions.md`](../agents/conventions.md) §5（分章约定整段替换为退役说明 + 统一口径）、`README.md` 管线描述同步。
- **未执行**：push 与合并（由用户确认）。

### 10.5 给后续票的遗留

- **#93**：三份产物的产物级统一（含上游注入字段、审核提示词细节、待确认清单的章节字段）。
- **#94**：清理兼容字段 / 死列（`completed_sections`、`live_by_section`、`failed_sections`、`partial` 的最终去留）与契约收口；`job_state.FinishPayload.completed_sections` 目前仍作为通用存储在（用例也还在用它），那是 #94 的范围。

---

## 11. 本轮：三份分批续写（#93）

### 11.1 范围

- **撞上限就接着写**：模型单次输出撞上输出上限（结束原因 `length` / `max_tokens`）时**自动续写下一批**，直到模型自己收尾（`stop` / 无 `finish_reason`）。判据只有一处、只看**模型返回的结束原因**（`backend/src/backend/services/stream_guard.py::extract_finish_reason` 读 `response_metadata` 的 `finish_reason` / `stop_reason`），**不设字数阈值**——阈值会在偏短的文档上把一次正常生成拦腰截成两批，也会在该续写时放过。适配层无需改动：方舟封装早已把 `finish_reason` 记进 `response_metadata`（含「用量帧没有 choices」那条路径）。
- **续写循环只有一份实现**：`backend/src/backend/services/stream_reply.py::iter_continuation_events`。三份产物（PRD / 接口文档 / 提示词套件的整篇撰写）全部改调它，`continuation_messages` 由 `document_service._continuation_messages` 统一构造（原提示词 + 已写正文尾部 + 一句「接着写、不要重复」）；总额上限 `MAX_CONTINUATION_BATCHES = 6` 批。
- **接缝不重复、不漏字**：`_SeamGuard` 先扣住每批开头，与已写正文做逐字最长后缀匹配，**只丢掉「紧挨在正文末尾、逐字存在」的那一段**，其余原样吐出（扣住的字符在批次结束时一定交出去，含失败路径）。因此「不漏字」是构造性的——除了可证明的重复，正文一个字节都不动。提示词里的「不要重复」负责「换个说法复述」，代码负责「逐字重复不会出现在正文里」，两件事都要。
- **批次级重试**：某一批写到一半断流（可重试的传输层失败）时**在同一批里从断点再要一次**（`MAX_BATCH_ATTEMPTS = 3`，不落库、不等待、不碰任务状态）；重试用尽则整篇失败（`error`，不发 `done`），已生成的部分留在事件流里，照旧由 #85 落成一版 `partial=true` 的正文（服务层与任务通道两条接缝各有用例）。
- **续写过程对外可见**：每起一批补发一条 `stage(writing)` + `data.continuation`（第几批）。它**不是新阶段**：进度条不动、正文缓冲不清空、`outlining` 不会再发；前端读这个字段在生成页说一句「正文较长，已自动续写到第 N 段」，**不渲染成新章节**。

### 11.2 本期不做

- 审核 / 改写 / 修复 / 澄清改写**不续写**：这些链路要的是「整段结果」（整体 JSON 或整篇正文），截断续写只会产出两段互相矛盾的判定，仍按原口径抛 `OutputTruncatedError` → `error`。
- 不引入调度循环，也不改 #88 的任务级退避：批次级重试不改变任务状态，两者不在同一层（见 §9.8）。
- 不改表结构（无迁移），不给 `continuation` 落库：它是**过程信息**，重连拿不到就当没发生过（正文与阶段仍然正确）。

### 11.3 验收与证据

1. **撞上限后自动续写，最终得到完整正文**：`test_truncated_batch_is_continued_until_the_document_is_finished`（`length` / `max_tokens` 两种标记各一条）、`test_a_batch_that_ended_normally_is_not_continued`（正常收尾不续写）、单元接缝 `test_the_loop_reports_the_final_reply_in_done`。
2. **批次之间不重复、不漏字**：`test_continuation_does_not_repeat_the_previous_ending`（模型重起头重复上一批结尾 → 用户可见正文里只出现一次）、`test_continuation_repeats_nothing_when_the_model_keeps_writing_forward`（不重合时一个字节都不动）、`test_a_shared_word_across_the_seam_is_not_eaten_twice`（接缝上共用词只留一份）、`test_the_continuation_request_does_not_carry_the_whole_document`（只带尾部、不带全文）。
3. **某批失败在批次内重试，用尽则整篇失败并保留已生成部分**：`test_a_failed_batch_is_retried_within_the_batch`（同一批重试、从断点继续）、`test_a_batch_that_keeps_failing_fails_the_document_and_keeps_the_written_part`（`error` 且不撤已写正文）、`test_continuation_is_bounded_so_a_runaway_model_cannot_loop_forever`（批数护栏）、端到端 `test_exhausted_batches_land_the_written_part_as_a_partial_version`（任务失败 + `document_versions.partial=true` 且正文含已写部分）。
4. **续写过程对外可见，进度持续推进**：`test_continuation_is_visible_and_never_rewinds_the_stage`（一批一条 `writing`、`continuation` 递进、`outlining` 不再回头）；前端 `continuation.spec.ts`（阶段条仍是五格且高亮「生成」、正文只出现一次、`continuation < 2` 不出提示、`resetPreview` 归零），`api.spec.ts` 的「stage 事件带续写批次序号」守住解析。
5. **三份产物行为一致**：`test_all_three_products_continue_the_same_way`（三份产物参数化：同样两批、同样接缝、同样事件形状）。

### 11.4 复测与收口

- 后端：`cd backend && PRDWRIGHT_TEST_POSTGRES_URL=…:5435 uv run pytest -n auto -q` → **1460 passed，0 skipped，0 failed**（#92 收口基线 **1443 passed**；本票净增 17，全部在新增的 `backend/tests/test_continuation.py`）。`--collect-only -q` 现取 **1460** 收集 / **1260** 文本计数 / **200** 参数化展开差额。
- 前端：`cd frontend && npm run test:unit -- --run` → **758 passed / 43 文件**（#92 基线 **751 passed / 42 文件**）；`npm run build`（含 `type-check`）通过。
- 文档：本文件 §11、[`ARCHITECTURE.md`](./ARCHITECTURE.md) §3.3（新增「分批续写」条）、`GLOSSARY.md`（新增「分批续写」词条并写明与批次内重试 / 任务级退避的层级差别）、[`../agents/conventions.md`](../agents/conventions.md) §5（新增口径条目）。
- **未执行**：push 与合并（由用户确认）。

### 11.5 给后续票的遗留

- **#94**：`failed_sections` / `partial` / `completed_sections` 等兼容字段的最终去留；`continuation` 目前只在事件里（不落库、不进快照），若将来要在重连后也显示「写到了第几批」，需要给快照加一格——那是契约变更，留给收口票一并定。
- **审核 / 改写的续写**：若将来确实需要（例如极长的接口文档），要先决定「续写出来的 JSON 怎么合并」，不能照搬正文续写。

---

## 12. 本轮：审核 / 改写 / 修复统一为整篇（#94，spec #75 收口）

### 12.1 范围

- **一条审核 / 改写 / 修复路径，三份产物共用**：PRD / 接口文档 / 提示词套件的审核走 `backend/src/backend/services/document_review.py` 同一段代码（同一份提示词、同一个解析、同一个 `review_data` 形状），产物差异只由 `ProductContext` 表达。结论形状对三份**逐字相同**：`kind / verdict / summary / issues / rewritten / reviewed_at / attempted`。
- **一版一条**：结论随 `document_versions.review` 保存；重新审核只更新**当前版本**那一条（`document_store.commit_review` + `expected_content_sha` 条件写），不追加版本、不改正文；正文在审的过程中被改过 → 任务失败（`CONTENT_CHANGED`）、原结论原样保留。
- **只有 high 触发改写，改写后不重审**：门槛判据只在 `review.triggers_rewrite(settings.review_rewrite_min_severity)` 一处，默认 `high`；medium 照实报给用户但不动笔。改写仍只重写受影响的那几节、拼回整篇后**整段覆盖**（`part{replace}`），定位不到时退回整篇重写。
- **修复 = 整篇重写 + 追加版本**：`document_service.fix_document_stream` 一次整篇重写 → 一次只审不改 → 收尾走 #81 的唯一写入入口 `document_store.write_document(mode="append")`（`source=review_fix`）。旧版正文与它自己那条结论一起留在版本历史里。
- **审核失败不写结论**：三种「没审成」分开（重审失败 → 任务失败、结论不写；修复的重写失败 → 正文与版本都不动；修复的重写成功而重审失败 / 生成时的审核降级 → 正文照落库、结论留空并写 `review_failed_at`）。「没审」与「审失败」由事件上的显式 `attempted` 标记区分，不判空串。
- **删前几票的死字段**：`failed_sections`（结论 / `RunSummary` / 快照 / 运行时四处恒空）整条链路删除；`completed_sections` 先在 #92 停写、本票用迁移 `0008` **删列**并撤掉 `FinishPayload` 与仓储参数；`review_defects.defects_count_text`（为分章汇总结论准备、零调用）删除；前端 `FailedSection` / `failedSections` / `sectionId` / `sectionTitle` 四个死形状删除（`review.ts` 的 `where` 保留对旧结论 `section_title` 的读回退）。
- **前端收口**：`ReviewConclusionPanel` 就是「整篇一条结论」——三份产物同一个面板、同一套按钮条件（修复只在「有问题」可点，重新审核只在其余三态可点）；删掉「本次有 N 章生成失败」那一块分章遗留提示。缺陷仍按类分组、类内按严重度排，旧结论没有类别仍归「未分类」。

### 12.2 本期不做

- 审核结论的历史版本浏览、结论进交付包 / 管理端统计、按正文变化自动判定结论过期（同 §9「本期不做」）。
- 审核 / 改写 / 修复**不续写**（#93 已定：这几步要的是整段结果，截断续写会产出两段互相矛盾的判定）。
- 分章历史数据不迁移：`completed_sections` 的旧值随列一起删（它只描述已退役的分章进度）。
- `continuation` 仍只在事件里、不落库、不进快照（若要重连后也显示「写到了第几批」，那是契约变更，另开票）。

### 12.3 验收与证据

1. **三份产物结论形状一致、一版一条**：`test_review_unify.py::test_conclusion_has_one_shape_for_all_three_products`（三份键集逐项相等）、`::test_one_conclusion_per_version_and_rereview_overwrites_it`（三份参数化：重审覆盖同一条、正文逐字不变、版本仍是 `[1]`、换会话后读到的是新那条）。
2. **只有 high 触发改写、改写后不重审**：`::test_only_high_severity_triggers_one_rewrite_and_never_rereviews`（三份参数化：审核调用恰好 1 次、high 改写 1 次且改写后不再出现 `reviewing`、medium 一次都不改但判定照实报）。
3. **修复整篇重写但追加版本**：`::test_fix_rewrites_whole_and_appends_a_version`（三份参数化：写入入口记录到 `mode="append"`、没有 `replace`、版本 `[2,1]`、来源 `review_fix`、旧版正文与旧结论都在）。
4. **重审只更新结论、不改正文**：`::test_rereview_keeps_the_old_conclusion_when_the_body_changed`（三份参数化：`error_code=CONTENT_CHANGED`、正文保留手改、原结论不动、版本数不变）；旧口径的两道闸门用例仍在 `test_review_job.py`。
5. **审核失败不写结论、界面照实显示失败**：`::test_failed_rereview_writes_no_conclusion_and_marks_the_failure`（三份参数化：任务失败、无 `review` 事件、`review` 列空、`review_failed_at` 落库、补审后清空）、`::test_attempted_marker_separates_not_reviewed_from_failed`（`attempted` 是「没审 / 审失败」的唯一分界，两条判定都是空串）。前端 `ReviewConclusionPanel.spec.ts` 的失败态用例仍守着「审核失败 + 失败时间、不渲染上一次结论」。
6. **死字段不许回流**：后端 `::test_chapter_leftover_fields_are_gone`（`RunSummary` / `GenerationJob` 模型字段 + `review_data` 键集）、`test_generation_job.py:1845`（快照里没有 `completed_sections`）与 `:2000`（没有 `failed_sections`）；前端 `dead-code.spec.ts`「分章遗留字段不许回流（#94）」。

### 12.4 复测与收口

- 后端：`cd backend && PRDWRIGHT_TEST_POSTGRES_URL=…:5434 uv run pytest -n auto -q` → **1478 passed，0 skipped，0 failed**（#93 收口基线 **1460 passed**；本票净增 18，全部在新增的 `backend/tests/test_review_unify.py`）。`--collect-only -q` 现取 **1478** 收集 / **1268** 文本计数（含新增文件）/ **210** 参数化展开差额。
- 前端：`cd frontend && npm run test:unit -- --run` → **760 passed / 43 文件**（#93 基线 **758 / 43**：删除 1 条「失败章节提示」用例、`review.spec.ts` 新增 3 条——定位回退与「结论里没有分章遗留字段」）；`npm run build`（含 `type-check`）通过。
- 迁移：新增 `0008_drop_completed_sections.py`（`down_revision="0007"`，只删一列，`downgrade` 按原定义加回、数据不恢复）。迁移 head = `0008`，`test_schema_migration` 的「基线库 == 模型库」仍成立。
- 文档：本文件 §12、[`ARCHITECTURE.md`](./ARCHITECTURE.md) §3.8（「三份产物同构」一段）与 §5.12（取舍）、`GLOSSARY.md`（「审核」「修复」「改写」三条按新行为改正）、[`../agents/conventions.md`](../agents/conventions.md) §5（删字段与单一面板口径）、[`state-flow-design.md`](./state-flow-design.md) 文末「实现落地差异」。
- **未执行**：push 与合并（由用户确认）。

### 12.5 spec #75 整体完成状态：19 张票全部落地

| 票 | 主题 | 状态 |
| --- | --- | --- |
| #76 | Postgres 接入（扩展期） | 已合入（`32d2cf1`） |
| #77 | 测试基座切 PG（每用例独立库） | 已合入（`4dede30`） |
| #78 | 收缩单一后端 + 迁移链并为 PG 基线 | 已合入（`7b8bea3`） |
| #79 | 状态机单一入口（三态先行） | 已合入（`36da093`） |
| #80 | 执行账本（`job_attempts`，迁移 0002） | 已合入（`1e6b48e`） |
| #81 | 原子收尾 + 统一版本锁 | 已合入（`7aff1dc`） |
| #82 | 租约 / 抢占 / 动作点续租（迁移 0003） | 已合入（`d7b89bc`） |
| #83 | 恢复器（过期租约收口） | 已合入（`4f668ff`） |
| #84 | 七态 + 取消中（迁移 0007） | 已合入（`5c73162`） |
| #85 | 部分完成落成一版正文（迁移 0004） | 已合入（`cf2b2a0`） |
| #86 | 事件表 + 游标订阅（迁移 0005） | 已合入（`8727973`） |
| #87 | 创建请求去重 | 已合入（`a117c24`） |
| #88 | 失败延迟退避（迁移 0006） | 已合入（`2c04b27`） |
| #89 | 管理端四个数字 | 已合入（`d511095`） |
| #90 | 回滚预案 + 可重复演练 | 已合入（`550308b`） |
| #91 | 大纲扩到三份 | 已合入（`8d3da77`） |
| #92 | PRD 整篇化 + 分章退役 | 已合入（`6a8c50c`） |
| #93 | 三份分批续写 | 已合入（`b17fa80`） |
| #94 | 审核 / 改写 / 修复统一为整篇（本票） | 本票 |

**判定**：19 张票全部落地，spec #75 可判为**已完成**。仍然悬着的是#75 范围之外或明确遗留的三条：① `backend/scripts/{schema_survey,auth_cli}.py` 是 SQLite 时代死脚本（#78 报告点名，需另票清）；② 去重**未加索引**（#87 显式遗留：判据带 10 秒滚动窗口，唯一索引表达不了）；③ 僵死取消与崩溃场景的用量 / 耗时仍记 0（#80 遗留）。三条都不改变「状态、收尾、审核结论、管线」的对外语义。

### 12.6 给后续票的遗留

- **`continuation` 不落库**（#93）：重连后看不到「写到了第几批」，要显示得给快照加一格——契约变更，另开票。
- **审核 / 改写的续写**：若将来确实需要（例如极长的接口文档），要先决定「续写出来的 JSON 怎么合并」，不能照搬正文续写。
- **PRD 的旧结论里可能只有 `section_title`**：前端 `parseReview` 已做读回退（`where` 优先、退回 `section_title`），后端 `conclusion_to_review` 同样回退；等历史数据自然淘汰后可以只留 `where`。

## 13. 本轮：spec #75 双轴评审修复（P0-1 ~ P3）

### 13.1 范围

`integration/spec-75` 的双轴评审（standards / spec）提出 11 项，本轮逐项收口。**没有新增迁移**：`head` 仍是 `0008`。

| 项 | 改了什么 |
| --- | --- |
| **P0-1** | 调度循环接上：`services/job_scheduler.py` 在 `lifespan` 末尾起三个 `asyncio` 周期任务（领取约 1 秒 / 收口约 30 秒 / 清理约 1 小时），关闭阶段第一步 cancel + await。`claim_once` / `recover_once` / `purge_expired_events` 第一次有了生产调用者——#83 AC1、#84 AC3、#88 AC1、#86 AC5 因此在运行期可达。派发前按库里的行重建任务单（重新解析上游输入、配置取当前生效值）；`chat` 无法重建上下文，如实判失败。开关 `job_scheduler_enabled`（生产默认开，测试默认关） |
| **P0-2** | 取消不再写 `review_failed_at`：用户按停止 ≠ 审核失败（`job_finalize._closeout_once` 的 `cancelled` 分支只落部分完成正文） |
| **P0-3** | `cancelling` 不再被顶替：顶替的前态收窄为 `pending` / `running`（`SUPERSEDABLE_STATUSES`），内存侧的顶替标记同样跳过取消中的运行时 |
| **P0-4** | 删方案 / 删账号**单步收口**：任意非终态 → `cancelled` + 清租约（`UPDATE … RETURNING` 带回逐条 id，账本照记）。收口前先把在途运行时的半截正文落一次盘（#85 的口径不回退） |
| **P1-1** | 三份产物的整篇生成循环收成**一份**（`document_service._stream_whole_document`），error 分支也 `flush()` 抑制器——失败落的那版「部分完成」不再丢尾字；同时消掉约 110 行重复 |
| **P1-2** | `admin_repo._count_one_day` 返回 frozen dataclass（`_DayCounts`）并按列名取值，不再按位置下标读聚合行 |
| **P1-3** | 失效注释 / 口径改正：`core/limits.py`、`generation_job_repo.py`、`services/job_events.py` 不再把 `completed_sections` 当现存列；前端 `ReviewStage.vue` / `types/index.ts` / `stores/runs.ts` 不再讲「失败占位的章节」 |
| **P2** | US10 落字：**取消只保证结果不再写入，不保证停止上游计费** → `GLOSSARY.md`「取消 / 已取消」词条 + `ARCHITECTURE.md` §4.5（契约 C-C1）；`state-flow-design.md` §11 补「调度循环已接」「四列用账本行数 / `max(id)` 等价替代」「方案删除单步收口」 |
| **P3** | 范围蔓延交代：`0007` 的旧值映射**保留**并写明理由（幂等、对空库零影响，见 `ARCHITECTURE.md` §5.7）；「分节改写」判定为「按缺陷精确定位到小节」而非分章遗留，**保留**（区别写在 `ARCHITECTURE.md` §5.12 与 `state-flow-design.md` §11）；`#77` 的「每用例独立 schema」在 `docs/agents/conventions.md` §10 与 `tests/conftest.py` 里改正为「每用例独立库（`CREATE DATABASE … TEMPLATE`）」 |

### 13.2 本期不做

- **不新增迁移**（无新列）：`attempt_count` / `max_attempts` / `snapshot_cursor` / `progress_snapshot` 四列仍用「账本行数 / `max(id)` / 既有快照列」等价替代。
- **不改启动档判据**：启动回收仍把上一次进程留下的非终态行判终态（它跑在调度循环之前，且那些行是残骸）。
- **不动 `deploy/`**（#78 / #90 的成果）与 `#77` 的建库机制。

### 13.3 验收与证据

- 调度循环（P0-1）：`backend/tests/test_job_scheduler.py` —— `test_scheduler_claims_a_due_pending_job_and_runs_it`（到期行被领走跑完 + 未到期行不动）、`test_scheduler_recovery_requeues_an_expired_running_job`（过期租约收口回排队并记账）、`test_lifespan_starts_and_stops_the_scheduler_cleanly`（三支周期任务，关闭后全部 `done()`、观测句柄清空）、`test_scheduler_is_off_by_default_in_tests`（开关）。
- 取消语义（P0-2 / P0-3 / P0-4）：`backend/tests/test_cancel_semantics.py` —— `test_cancelled_review_does_not_mark_review_failed`、`test_new_registration_keeps_a_cancelling_job_out_of_superseded`、`test_delete_solution_settles_a_running_job_in_one_step`；口径修正同步在 `test_job_state_machine.py`（批量取消 / 顶替前态）与 `test_job_attempt_ledger.py`（批量取消当场记账、单条取消确认时记账）。
- 失败分支 flush（P1-1）：`backend/tests/test_pending_items.py` 四条（PRD / 接口文档 / 提示词 / 短正文各一条）+ 三份产物共用一个循环函数。
- 单日计数投影（P1-2）：`backend/tests/test_admin_stats.py::test_daily_counts_projection_is_a_frozen_dataclass`。
- 文档与口径（P2 / P3）：`backend/tests/test_closeout_docs.py`（文档路径可见、无悬空引用）与 `test_architecture_guards.py` / `test_cleanup_dead_code.py` 全绿。

### 13.4 复测与收口

- 后端：`cd backend && PRDWRIGHT_TEST_POSTGRES_URL=…:5433 uv run pytest -n auto -q` → **1491 passed，0 skipped，0 failed**（spec #75 收口基线 **1478 passed**；本轮净增 13：调度循环 4、取消语义 3、失败分支 flush 4、单日计数投影 1、单条取消账本 1）。`--collect-only -q` 现取 **1491** 收集 / **1281** 文本计数 / **210** 参数化展开差额。
- 前端：`cd frontend && npm run test:unit -- --run` → **760 passed / 43 文件**（与 #94 收口基线一致：本轮只改注释）；`npm run build`（含 `type-check`）通过。
- 管理端：`cd frontend_admin && npm run test:unit` → **125 passed / 13 文件**；`npm run build`（含 `type-check`）通过。
- 迁移：**没有新增**。`head` 仍是 `0008`，`test_schema_migration` 的「基线库 == 模型库」仍成立。
- 文档：本文件 §13、[`ARCHITECTURE.md`](./ARCHITECTURE.md) §3.3（调度循环那条 + 取消 / 顶替口径）、§4.5（C-C1）、§5.7（取舍）、[`state-flow-design.md`](./state-flow-design.md) §11、[`GLOSSARY.md`](../../GLOSSARY.md)（「取消 / 已取消」）、[`../agents/conventions.md`](../agents/conventions.md) §10（#77 口径 + 循环在测试里默认关）。
- **未执行**：push 与合并（由用户确认）。
