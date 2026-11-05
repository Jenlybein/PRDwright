# PRDwright 现状排期

> **排期真源**：本文件列出 #31~#41 各票的落点与验收方式；架构结论与设计契约见 [`ARCHITECTURE.md`](./ARCHITECTURE.md)，阶段规模与界碑见 [`README.md`](./README.md)（时间线真源），本次文档重组的架构决策见 [`../adr/0001-architecture-docs-consolidation.md`](../adr/0001-architecture-docs-consolidation.md)。
>
> 由 v9 那一版排期收敛为现状排期：先文档、后代码；其中 T19.0~T19.7 是已交付的历史排期，不再是现状。

---

## 0. 排期总原则

1. **先文档、后代码**：#31 先把 v1~v9 九层压成一版现状文档 + 决策档案，#32~#33 把词表与收口纪律对齐，之后才动代码（#34 起）。
2. **1200 行阈值作废**：文件边界改以**接缝**为依据，取代 v8 附录 A §5 的 W1 口径（见 [`ARCHITECTURE.md`](./ARCHITECTURE.md) §5.4）。七个实现文件归入同一内部包即可，不必合并回单个大文件。
3. **数字先复核再写**：对外动作与名字数量、入口模式同步点数量一律以实测为准，不沿用旧报告里的约数；写文档前 `git grep` 复核。
4. **用例基线现取现算**：`cd backend && uv run pytest --collect-only -q` 取当前收集数；旧文档里的 1089 / 1100 / 1110 / 1127 / 1136 / 1140 / 1149 / 1150 / 1175 及「文本计数 955 / 参数化展开差额 134」整段一起重算，不沿用中间数。#41 终检实测：加入终检守卫前收集数 1175、`def test_` 文本计数 1005、参数化展开差额 170；加入 4 条终检守卫后的终值 1179 / 1009 / 170，实测口径见 `docs/agents/conventions.md` §10。
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
```

**先文档、后代码**的分界在 #33 / #34 之间：#31~#33 不动生产代码语义，#34 起才改生成任务与数据库投影。

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
| **#37** | 迁移批次一——生产调用方 + 排空职责归位 | `backend/src/backend/api/jobs.py`、`backend/src/backend/main.py`、`backend/src/backend/services/job_drain.py`、`backend/src/backend/api/health.py` | 等候在途任务交回生成任务一侧；排空只问「还有哪些在跑 / 要不要等 / 超时取消哪几支」，不读运行时内部字段；30 秒窗口、摘流、就绪探针、可直接调用形态都不变；启动回收孤儿任务不变 |
| **#38** | 迁移批次二——测试全链路用例改走主接缝 | `backend/tests/test_generation_job.py`、`backend/tests/test_chat_job.py` 等全链路用例 | 断言只跨「造单 → 登记 → 派发 → 订阅到结束」主接缝；假环境在造单时传入（生产真值 / 测试假值）；旧的裸运行时用例替换而非叠加；不新增测试专用私有缝 |
| **#39** | 迁移批次三——登记 / 收尾 / 快照 / 裸运行时用例归位 | `backend/tests/` 中登记、收尾、数据库兜底快照、直接构造运行时四类用例 | 只造单不派发的用例归到登记语义（顶替、目标产物落列）；只派发不登记的用例归到造单 + 派发；直接构造裸运行时的用例全改成跨公开动作断言；方案被删时收尾中止、失败不覆写正文仍成立 |
| **#40** | 收缩——删旧入口、再导出与内部包边界落定 | `backend/src/backend/services/job_service.py`（包入口）、七个实现文件、`backend/src/backend/repositories/` | 包入口 `job_service.__all__` 实测只露 9 个正常动作（#37/#38 迁走 `create_job` / `dispatch` 后），新三步 `job_ticket` 与「等待在途」`job_wait` 各有自己的公开面；再导出整块删空、主文件不再当中转；两个仍被生产使用的名字各归其位、调用方从真正所属处引用；排空留在包外；单 worker + 进程内运行时不变 |
| **#41** | 文档 / 路线图 / 词表 / 基线与引用终检 | `docs/architecture/ARCHITECTURE.md`、`docs/architecture/roadmap.md`、`docs/agents/conventions.md`、`backend/pyproject.toml`、`backend/scripts/schema_survey.py`、`backend/tests/test_closeout_docs.py` | 代码、架构文档、路线图、词表与决策档案结论一致；用例基线按收集数现取现算（连文本计数 / 参数化展开差额整段重算）；数字逐条 `git grep` 复核；全仓无悬空引用、文档写出的路径逐条可见；路径扫描白名单按类别写明依据；全量离线跑绿；`git add -An .` 预演并经人确认后才提交 |

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
- 不重开已关闭的双后端系列；唯一部署后端仍是 SQLite。
- 不在仓库里落规格文件或临时工单目录；规格与票住在 GitHub Issues。

---

## 5. 验收总表（#41 收口时逐条打勾）

| # | 判据 | 怎么验 |
| --- | --- | --- |
| 1 | 架构结论只有一版现状文档 | `ls docs/architecture` 只有 `README.md` / `ARCHITECTURE.md` / `roadmap.md` |
| 2 | 决策档案从 0001 重新编号、只有一条 | `ls docs/adr` |
| 3 | 全仓不再有指向九层目录的悬空引用 | `git grep -n -E 'v[1-9]/(ARCHITECTURE\|README\|roadmap)\.md'` 无命中 |
| 4 | 文档写出的每个路径都能看见 | 一次性脚本扫反引号里的仓库路径，逐条 `Test-Path` |
| 5 | 三段承重专题整段在现状文档里 | 按附录 A / B / C 标题定位核对 |
| 6 | 全量用例离线跑绿、零 LLM Key | `cd backend && uv run pytest`；基线按 `--collect-only -q` 现取现算 |
| 7 | 代码、架构文档、路线图三者同步 | 逐票对照 §2 的落点表 |
| 8 | 提交前预演并经人确认 | `git add -An .`，确认无 `.env` / `.venv` / `node_modules` / `dist` |
