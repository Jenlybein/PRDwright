# PRDwright 架构演进与重构路线图（2026-10-13 重订 / 2026-10-14 进度同步）

> 状态：**原 P0–P4 已全部落地，本版只规划「剩余工作」**。
> 依据：2026-10-13 代码库实测（后端 5676 行 / 26 模块；前端 14652 行 / 46 文件；后端 78 用例、前端 209 用例）。
> 配套文档：`ADR.md`（13 条已决记录，含 ADR-011 双闸门、ADR-012 错误契约、ADR-013 统一流式编排）。
> 前提交代：终局是**少量人 + 简单登录**，该目标已通过 owner 过滤 + 登录系统提前兑现；本版聚焦「把剩下的可靠性与部署补齐」。
>
> **2026-10-14 进度更新**：R0-T1 CI ✅（`.github/workflows/ci.yml`，云端双 job 已跑绿）、
> R0-T5 错误码化 ✅（`7be5989`）、R0-T3 流式去重 ✅（四份重复 → `services/stream_reply.py`）。
> **R0 安全网全部完成**；用例数为**后端 117 / 前端 222**。下一阶段 R1 生成可靠性。

---

## 一、现状盘点（实测，非旧文档推断）

### 1.1 已落地（对照旧路线图逐条核查）

| 旧阶段 | 实际状态 | 证据 |
| --- | --- | --- |
| P0-T1 后端测试 | ✅ 78 用例 | `backend/tests/` 7 文件，含认证门禁不变量 |
| P0-T2 前端测试 | ✅ 209 用例 | `frontend/src/__tests__/` 15 文件，`--run` 全绿 |
| P0-T4 删死接口 | ✅ | OpenAPI 路径已收敛，无 `NotImplementedError` 路由 |
| P1 方案持久化 | ✅ | `repositories/solution_repo.py` + `services/solution_service.py` + `api/solutions.py` + 前端 `stores/solutions.ts` + 左侧栏 + `vue-router` 两条页面级路由 |
| P2 前端解耦 | ✅ | `App.vue` 已物理删除；`AppV2.vue` 只做分派；`stages/` `form/` `ui/` 组件库；`stores/workflow.ts` 状态机纯函数；`composables/` |
| P3-T1 分章节生成 | ✅ | `services/prd_section_writer.py`：`PRD_SECTIONS` + 逐节流式 + 前向上下文 `prior_sections` |
| P3-T3 max_tokens 分档 | ✅ | `services/llm_factory.py` 按文档类型配置 |
| P4 登录 | ✅ | `api/auth.py` + `auth_service` + `auth_repo` + 纯 ASGI 默认拒绝中间件 + 前端 `AuthDialog`/`UserMenu` + `scripts/auth_cli.py` |
| **R0-T1 CI** | ✅ 2026-10-14 | `.github/workflows/ci.yml`：backend（uv + pytest）/ frontend（npm ci + vitest `--run` + build）双 job 并行，云端已跑绿 |
| **R0-T5 错误码化** | ✅ `7be5989` | `core/errors.py`（`ErrorCode` 8 码）+ `main.py::register_error_handlers` + 前端 `services/errors.ts`；前后端码值一致由不变量测试守卫 |
| ADR-007/008/009/010 | ✅ 已决已落地 | 见 `ADR.md` |

### 1.2 路线图外、期间新增的能力（非债务，但影响部署与文档）

- **双闸门工作流**（5766e7d）：闸门 1「摘要 Diff」（`sync-summary-from-conversation` → `SummaryDiffPanel` 确认才回写）；闸门 2「RAG 接口规范检索」（`retrieve-api-docs-rag`，本地词法检索 `core/api_doc_knowledge.json` → `RagResultPanel` 勾选）。
- **三入口模式**（1636eaf）：完整流程 / 快捷入口 / 调试入口。
- **MVP 留空推断**（babc4a6）：`requirements` 为空时按 `ui_pages` 推断候选功能清单并标「（建议）」。
- **主题系统**（1625e3c）：亮/暗/跟随系统三态，三层语义令牌。
- **RAG 知识库资产**：`core/api_doc_knowledge.json` 是**新增的部署资产**，Docker 化时必须随镜像打包（见 R3）。

### 1.3 仍存在的债务清单（按新优先级）

| 编号 | 问题 | 位置 | 状态 | 归属阶段 |
| --- | --- | --- | --- | --- |
| ~~D2~~ | ~~流式编排四份重复~~ → **已解决**（2026-10-14，R0-T3）：全部收敛到 `services/stream_reply.py`（`astream_text` / `iter_reply_events` / `collect_reply` / `error_event`），三个 service 改为调用；`_extract_text` 副本与 `_error_event` 包装已删除 | 三 service | ✅ 已解决 | — |
| ~~D4~~ | ~~错误契约靠中文串~~ → **已解决**（2026-10-14，`7be5989`）：判据改为 `isApiError(error, 'SESSION_NOT_FOUND')`，并由 4 条不变量测试防回退 | 前端 composable | ✅ 已解决 | — |
| D7 | **长文生成不可续传**：`prd_section_writer` 逐节生成但是**单次运行内**的内存态，网络中断仍整体丢失；无中间产物落库 | `document_service` / `prd_section_writer` | ❌ 仍开 | R1-T2 |
| D8 | **技能目录仍脆弱**：`PRDWRIGHT_SKILLS_DIR` 已支持，但缺失时仍「向上找目录」并**静默降级**到内置提示词（D8 未真正解决） | `prd_skill.resolve_skill_dir` | ⚠️ 部分 | R2-T6 |
| D9 | **无可观测性与成本计量**：仅 `/health` 返回 `{"status":"ok"}`；无任何 token 打点、request_id、结构化日志 | 全局 | ❌ 仍开 | R1-T5 / R2-T7 |
| D10 | **技能资源常驻缓存**：`prd_skill.py:64 @lru_cache` 缓存到进程结束，改模板必须重启 | `prd_skill` | ❌ 仍开 | R2-T6 |
| D11 | **零部署**（CI 已解决）：`.github/workflows/ci.yml` 已就位并跑绿，测试不再靠人手跑；仍无 `Dockerfile` / `docker-compose` / `Makefile` | 仓库根 | ⚠️ 部分（部署待 R3） | R3 |
| D12 | **无供应商 failover**：`llm_factory.get_llm` 只按 `default_llm_provider` 构造单个模型，无主备切换/超时重试 | `llm_factory.py:31` | ❌ 仍开 | R1-T4 |

> 已彻底解决的旧债务（不再追踪）：D1 状态双真相（P1）、D3 死接口、D5 App.vue 巨石（P2）、
> D6 零测试、**D4 错误契约靠中文串（R0-T5，2026-10-14）**。

---

## 二、已经做对、明确不要动的设计

重构最容易犯的错是把好的一起推翻。以下仍是全项目最干净的部分，**任何阶段都只复用不替换**：

- **`llm_factory` 的三供应商抽象**：ark / anthropic / openai 统一 `astream`，业务层无感——这是 R1-T4 failover 的天然接入点。
- **路由层 / service 层 / repository 层分离**：`api` 只解析封装，`schemas.py` 集中契约，`repositories/` 是全项目唯一写 SQL 的地方（review 红线）。
- **SSE 事件协议**：`status / chunk / part / stage / done / error` + 末尾 `[DONE]`，两层异常兜底；PRD 正文以 `part` 事件为准（预览与最终同源，见 8ea4896）。
- **技能包驱动 + 自动降级**：`skills/prd-generator` 缺失时退回内置提示词，服务不中断（R2 只把「静默降级」改成「显式告警 + 可选 fail-fast」）。
- **默认拒绝中间件**（纯 ASGI，避开 `BaseHTTPMiddleware` 对 SSE 的响应体重包）+ 遍历 OpenAPI 路径的不变量测试。
- **快照范式 + owner 强制过滤**（ADR-009）：单表全量 JSON、`Repository` 不接受 `owner_id=None`。

---

## 三、重订路线图（剩余工作 R0 → R3）

排序原则升级为：**先给手上的测试装上自动执行的网（R0，现 329 个），再补核心交付物的可靠性（R1），再工程化资产与可观测性（R2），最后把「能跑」变成「能部署」（R3）。**

> 命名 `R0–R3`（Revised）是为了和已完成的 `P0–P4` 区分——它们不是推翻重来，而是收尾。

---

### R0 · 安全网收尾：CI + 两个未结的护栏项

**周期**：3–5 天（T1/T5 已完成，仅剩 T3）　**功能变化**：零　**风险**：低（T3 需对照测试）

**目标**：在动任何有风险的结构之前，先把「测试靠人手跑」和「两个半吊子护栏」解决掉。

| 任务 | 具体动作 | 状态 |
| --- | --- | --- |
| T1 建 CI | GitHub Actions 双 job：backend `uv sync` + `uv run pytest`；frontend `npm ci` + `npm run test:unit -- --run` + `npm run build`（含 `vue-tsc`）。push/PR 触发，红灯拦合并 | ✅ 已落地并跑绿 |
| T5 错误码化 | `code` 与 `detail` 并列（`detail` 一字不改），8 个机器码；**SSE `error` 事件也带顶层 `code`**（原判据来自流内而非 HTTP 响应，见 ADR-012）；前端改判 `isApiError(error, 'SESSION_NOT_FOUND')` | ✅ `7be5989` |
| T3 收尾流式去重 | 把四份重复（两份 `_stream_reply` + `_error_event` + `_extract_text` 副本）收敛到 `services/stream_reply.py` 一处，三个 service 改为调用它；`stream_guard.py` 保持只管截断检测。**先写对照测试固定当前行为，再统一**（详见 ADR-013） | ✅ 已落地 |

**验收**：
- ✅ 推一个空 PR，CI 能红灯拦下「漏跑测试 / 类型错误」。
- ✅ 前端源码零中文错误串判据（由 `tests/test_error_contract.py` 的扫描不变量永久守护）。
- ✅ 合并后：`grep -rn "async def _stream_reply\|def _extract_text" backend/src/backend/services` 归零（由 `tests/test_stream_reply.py::test_services_hold_no_duplicate_stream_implementation` 永久守护）；
  合并前用**差分比对**跑了 12 组场景（正常多块 / 单块 / 截断 / 空输出 / 中途异常 / 结构化分块 / 无 status_text），新旧实现**逐事件完全一致**。

**不做**：不碰持久化、不改 UI、不动提示词内容。

**风险**：T3 合并时两份实现的边角行为可能不一致（`on_finish` 的调用时机、异常后是否还发 `done`）。这是旧路线图 P0-T3 遗留的坑，**必须先用对照测试钉死**再动。

> ⚠️ **规模更正（2026-10-14 实测）**：此前把「`conversation_service.py:366` / `document_service.py:626`」的**行号**误读成了行数，
> 一度把 T3 描述为「365 / 625 行的大手术」。实测两个函数本体分别只有 **44 行**与 **48 行**，
> 且签名只差一个 `on_finish` 参数。T3 的真实风险不在体量，而在**两处调用点各自依赖的边角行为**。

---

### R1 · 生成可靠性：核心交付物「放心用」

**周期**：2–3 周　**风险**：中（R1-T2 改动生成主链路）

**目标**：把「能生成 PRD」升级成「中断可续、供应商挂了能切、花了多少 token 看得见」。

| 任务 | 具体动作 | 关联债务 |
| --- | --- | --- |
| T2 断点续传 | 在 ADR-009 快照内新增 `prdSections` 中间态字段（已完成节的标题→正文）。生成请求带 `resume_from` 时，只补生成缺失节，已完成的节不重复消耗 token。单节失败只重试该节 | D7 |
| T4 供应商 failover | `llm_factory` 接受**有序供应商列表**（`default_llm_provider` + `fallback_llm_providers`）；调用抛异常或超时，按序切下一个；业务层无感。`get_llm()` 返回封装了重试/切换的客户端 | D12 |
| T5 成本计量 | 每次生成从 `response_metadata` 取 `prompt_tokens` / `completion_tokens`，随方案快照落库（或在 `solutions` 冗余列记总数）；方案详情可看「本次生成 token 与所用供应商/模型」 | D9（成本部分） |

**验收**：
- 生成中途 kill 后端网络/进程，恢复后能从断点续，已完成节不重复计费。
- 把主供应商 key 置空，自动切备用供应商，业务代码零改动。
- 方案详情页能查到最近一次生成的 token 总量与模型名。

**不做**：不做异步任务队列（当前同步流式体感更好）；不做多轮自评/批判式生成（先把单次可靠性做扎实）。

**风险**：T2 续传会改变 PRD「一气呵成」的连贯性——保留「整篇模式」开关（ADR-004 已定），两种模式由配置切换，续传仅作用于分章节模式。

---

### R2 · 资产工程化与可观测性

**周期**：1–2 周　**风险**：低

**目标**：让提示词/技能资产可调试、可审计、可部署；补上出问题时能归因的最低限度观测。

| 任务 | 具体动作 | 关联债务 |
| --- | --- | --- |
| T6 技能资产加固 | 开发态：弃用 `lru_cache` 常驻缓存，改为「按文件 mtime 的轻量缓存 / 显式重载开关」，改模板即时生效（D10）。生产态：`PRDWRIGHT_SKILLS_DIR` 从「可选兜底」提升为「缺失即启动失败」（fail-fast），消除静默降级（D8）。每次生成记录所用技能资源版本/指纹 | D8 / D10 |
| T7 可观测性基线 | 结构化日志（JSON 行）+ 每请求 `request_id`（中间件注入 `scope`）；`/health` 扩展返回 DB 连通性 + 主/备供应商可用性 + 技能目录是否就位 | D9（观测部分） |
| T8 RAG 资产登记 | `core/api_doc_knowledge.json` 纳入「部署资产清单」，与 `skills/` 一起作为 Docker 打包项；记录其版本/更新时间，避免部署后检索静默失效 | 新能力收口 |

**验收**：
- 改 `prd-template.md` 无需重启即生效（开发态）；故意把 `PRDWRIGHT_SKILLS_DIR` 指错目录，后端启动即报错而非降级。
- 任意一次请求日志含 `request_id`；`/health` 能反映 DB 与供应商健康。
- Docker 镜像内 `skills/` 与 `api_doc_knowledge.json` 路径在生产配置下可被加载（R3 验证）。

**不做**：不引入 Prometheus/Grafana（少量人场景，结构化日志 + `/health` 足够）；不做分布式追踪。

---

### R3 · 部署与多人加固

**周期**：1 周　**风险**：低（不动业务逻辑）

**目标**：把「本地能跑」变成「一条命令起全栈、CI 拦破坏性改动、两账号互不可见」。

| 任务 | 具体动作 | 关联债务 |
| --- | --- | --- |
| T3 Docker 化 | `Dockerfile`（后端 uv 多阶段 + 前端 `npm run build` 静态产物）+ `docker-compose.yml`（backend + nginx 反代前端，SSE 的 `credentials: same-origin` 与 nginx 同源部署天然兼容）。`skills/` 与 `core/api_doc_knowledge.json` 随镜像打包（承接 R2-T8） | D11 |
| T4 生产加固 | `AUTH_COOKIE_SECURE=true` 写入部署配置；轮换默认邀请码 `123456`；配置全部走环境变量，`.env` 不入库 | 旧 P4-T6 |
| T5 部署态隔离验证 | 容器里建两个账号，互访问对方方案 id 返回 404；侧边栏互不可见（owner 过滤在 Repository 层强制，已在 P1 落地，这里只做部署态复验） | 旧 P4-T2 |

**验收**：
- `docker compose up` 一条命令起全栈，浏览器同源访问可用，SSE 流式正常。
- 默认邀请码不再是 `123456`；Cookie 在 HTTPS 下带 `Secure`。
- 两账号互不可见，CI 红灯能拦住破坏性改动。

**不做**：不做多实例会话共享（当前单实例足够；真要扩再换 `TokenIssuer` 实现，ADR-007 已留接口）；不做计费、角色权限、租户隔离。

---

## 四、为什么是这个顺序（含被否决的替代方案）

| 决策 | 理由 |
| --- | --- |
| **CI 提到 R0 而非留在 R3** | 旧路线图把 CI 放在部署阶段，但当时已有 287 个测试且 R0-T3 要动有风险的流式去重代码。先装自动执行的网，才能让后续每个改动「敢动手」——这是旧 P0 护栏思想的延续，只是落地时机随测试规模而前移（现 329 个测试，CI 已跑绿） |
| **R0 先于 R1** | T3 流式去重是已知风险点，必须先有 CI 兜底 + 对照测试钉死行为；T5 错误码是仍活着的 bug（前端字符串匹配），已于 2026-10-14 收掉 |
| **R1 紧随 R0** | PRD 是核心交付物，中断即全废（D7）、供应商单点（D12）、成本不可见（D9）直接决定「用户敢不敢用」。它们改的都是生成主链路，应在结构稳定后集中处理 |
| **R2 在 R1 之后** | 资产工程化（热重载/fail-fast）与可观测性不阻塞任何功能，但 R2-T8 的 RAG 资产登记是 R3 Docker 打包的前置，故排在 R3 之前 |
| **R3 最后** | Docker/CI/加固不阻塞开发，等全栈形态（含 R2 的资产清单）稳定后一次性做，且 R2-T8 明确要打包的资产后，R3 才不会漏项 |

**被否决的替代方案**：
- **直接上 Postgres + K8s**：当前少量人 + 单实例，SQLite + Docker Compose 的运维成本近乎为零；切 Postgres 的代价被 Repository 隔离在一个文件内（ADR-002）。否决。
- **续传另建 `documents.sections` 表**：与 ADR-009「单表全量快照」冲突，会 reintroduce 差集同步复杂度。否决——续传中间态复用同一份快照 JSON 内的字段。
- **现在就做多轮自评/批判式生成**：先把单次可靠性（续传/failover）做扎实，再谈质量增强，避免把两个不确定因素叠在一起。

---

## 五、贯穿始终的检查点

每个阶段结束前自查：

1. **测试只增不减**：R0 起 CI 强制；每个阶段结束 `pytest` 与 `vitest` 用例数只增不减。
2. **依赖方向**：`api → services → repositories → db`，无反向 import；services 之间无横向 import（D2 收尾后流式编排只在 `stream_reply.py`）。
3. **契约一致**：后端 `schemas.py` 与前端 `types/index.ts` 字段名一致；**新增错误必须带 `code`**（R0-T5 后不得新增中文串匹配，由不变量测试守卫，见 ADR-012）。
4. **鉴权默认关闭风险**：新增接口若在白名单内必须说明原因；非白名单自动受保护，由 OpenAPI 路径不变量测试守卫（ADR-008）。
5. **owner 强制过滤**：任何查方案/文档的 Repository 方法都带 `owner_id`，不接受 `None`（ADR-009）。
6. **技能资产不静默降级**：R2-T6 后，资源缺失在启动或生成时显式报错/告警，不再悄悄退回内置提示词。

---

## 六、风险登记

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| R0-T3 合并四份流式重复时边角行为漂移 | 生成输出突变（`on_finish` 不写历史 / 异常后多发了 `done`） | 先写对照测试钉死 `status→chunk→done` 序列、异常转 `error` 后不发 `done`、`on_finish` 仅成功时调用，再统一 |
| R1-T2 续传导致章节间口径不一致 | PRD 质量下降 | 保留整篇模式开关；续传仅作用于分章节模式；前向上下文已传 `prior_sections` |
| R1-T4 failover 触发双倍调用成本 | 费用上升 | 仅主供应商异常/超时切备；正常路径零开销 |
| R2-T6 热重载引入竞态 | 读到半截模板 | 文件级原子替换 + mtime 比对，不做内容 diff |
| SQLite 部署态并发写 | 写入卡顿 | 开 WAL（P1 已做）；真不够经 Repository 切 Postgres |

---

## 七、明确不做

- 微服务 / CQRS / 事件溯源 / GraphQL：单体 + 9 条 REST+SSE 完全够用。
- 异步任务队列：同步流式体感更好，除非并发上来。
- 多租户 / 计费 / 角色权限：少量人场景，owner 过滤足够（ADR-009）。
- 自研 Agent 编排框架：LangChain 抽象当前够用。
- 多实例会话共享：当前单实例足够，真要扩再换 `TokenIssuer` 实现（ADR-007 已留接口）。

---

## 八、实施进度（截至 2026-10-14）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 原 P0 护栏（除 T3/T5） | ✅ | 用例数已增长为 **117 后端 / 222 前端**；死接口已删 |
| 原 P1 方案持久化 | ✅ | 快照范式 + owner 过滤 + 侧边栏 + router |
| 原 P2 前端解耦 | ✅ | App.vue 已删；组件库 + 状态机 + composables |
| 原 P3-T1/T3（分章节 + max_tokens 分档） | ✅ | `prd_section_writer` + `llm_factory` 分档 |
| 原 P4 登录 | ✅ | 认证 + 中间件 + 前端 + CLI |
| **R0-T1 CI** | ✅ | `.github/workflows/ci.yml`，双 job 云端已跑绿（107 + 222） |
| **R0-T5 错误码化** | ✅ | `7be5989`；ADR-012；4 条不变量测试防回退 |
| **R0-T3 流式去重收尾** | ✅ | D2 四份重复 → `services/stream_reply.py`；差分比对 12 组一致 + 10 例契约测试 |
| **R1-T2 断点续传** | ⬜ | D7 |
| **R1-T4 供应商 failover** | ⬜ | D12 |
| **R1-T5 成本计量** | ⬜ | D9 成本部分 |
| **R2-T6 技能资产加固** | ⬜ | D8/D10 |
| **R2-T7 可观测性** | ⬜ | D9 观测部分 |
| **R2-T8 RAG 资产登记** | ⬜ | 新能力收口 |
| **R3 Docker / 加固 / 隔离验证** | ⬜ | D11 + 旧 P4-T6 |

---

## 九、版本分期（2026-10-14 收口）

前面八节按「任务」组织，本节按「对外版本」组织——一次发版是一批任务的集合，
两者的对应关系在这里钉死，避免「v2 到底含什么」事后各说各话。

### v2 · 「能用」（已完成，tag `v2.0.0`，位于 `57b76ef` 之后的收口提交）

一句话：**一个能注册登录、能把需求变成 PRD/接口文档/提示词、能存方案、改坏了有 CI 拦着的产品。**

| 能力 | 内容 |
| --- | --- |
| 核心流程 | 三入口（完整流程 / 快捷入口 / 调试入口）→ 表单 → 对话澄清 → 双闸门（摘要 Diff + RAG 接口规范）→ PRD → 接口文档 → 提示词 |
| 生成 | 分章节撰写 + 审核 + 改写双 Agent（`prd_section_writer`）、`max_tokens` 分档、流式 SSE 实时预览 |
| 持久化 | 方案单表全量快照 + owner 强制过滤 + 左侧栏 + 防抖保存 |
| 账号 | 注册（需邀请码）/ 登录 / HttpOnly Cookie / 默认拒绝中间件 / 未登录禁止建任务 |
| 前端 | `App.vue` 巨石已拆（`AppV2` + `stages/` + `ui/` + `composables/`）、亮暗主题三层令牌、底栏为唯一操作落点 |
| 可靠性护栏 | CI 双 job（后端 117 例 / 前端 222 例，全程离线零 token）、错误契约机器码化（ADR-012）、流式编排单一实现（ADR-013） |
| 品牌 | HarnessPRD → PRDwright 全量更名（仓库根目录名保留 `HarnessPRD`） |

**v2 的边界**：以上能力都成立，但一次「长文生成」仍是**乐观流程**——中断即全丢、供应商单点、成本不可见。
这三条不是 bug，是 v2 明确没做的事，由 v3 承接。

### v3 · 「放心用」（进行中，起点 = R1 生成可靠性）

一句话：**长文生成中断可续、供应商挂了能切、花了多少 token 看得见，最后能一条命令部署。**

| 版本阶段 | 对应本文件章节 | 目标 |
| --- | --- | --- |
| v3.0 生成可靠性 | R1（T5 成本计量 → T4 供应商 failover → T2 断点续传） | 引入「生成运行记录 run」，三项共享同一份中间态 |
| v3.1 资产与观测 | R2（T6 技能加固 / T7 可观测性 / T8 RAG 资产登记） | 技能热重载 + fail-fast、结构化日志与 `/health` |
| v3.2 部署 | R3（Docker / 生产加固 / 隔离验证） | `docker compose up` 起全栈 |

> 版本阶段与任务编号（R1/R2/R3）是**同一套工作的两种视图**：任务编号用于日常排期，
> 版本号用于对外收口。一个 v3.x 通常对应一个 R 阶段的完成。

**发版约定**：
- 每个版本收口时打 annotated tag（`v3.0.0` 等），tag message 写明「含哪些任务」。
- tag 只在本地打，推送由维护者决定（`git push origin v3.0.0` 与代码 push 分开）。
- 收口提交前必须：后端 `pytest` 与前端 `vitest --run` 全绿、用例数只增不减、roadmap 本节同步更新。
