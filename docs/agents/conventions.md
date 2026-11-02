# PRDwright 工程约定（agent 必读）

> 一句话说明：这是从旧长期笔记蒸馏出的当前有效约定；失效规则见文末附录。
> 相关：`docs/architecture/`（各层设计与取舍）、`../../GLOSSARY.md`（术语）、`domain.md`（谁在读这些）。

## 1. 通用纪律

- 注释 / docstring / 提交信息用中文，标识符用英文；只用 uv（`uv run …` / `uv add` / `uv sync --frozen`），仓库没有 requirements.txt。
- 提交前必须用户确认：先 `git add -An .` 预演，确认无 `.env` / `.venv` / `node_modules` / `dist`；agent 不自行 commit。
- 架构文档正向写：目标 → 范围（含「本期不做」）→ 设计 → 契约 → 取舍；禁止写成「盘点了哪些缺口」。
- `code` 是唯一机器判据，`detail` 仅供展示：禁止用 `detail` 或中文做分支判断。
- 提示词只在后端（`services/prompts.py`、`skills/prd-generator/**`、`core/question_config.json`）；前端只能「点名」场景（`profile_key`），不能「递剧本」文本；用户可见文案不复述提示词数值。
- 前端守卫扫 `frontend/src/**` 全文（分不出注释与代码）：注释里写「等号紧跟中文」也会红。
- 文档里写出的每个路径，`ls` 必须能看见；写完 `ls` + `git status` 复核并尽快提交（本项目两次出现「报告写入成功、次日消失」）。
- 品牌 PRDwright，仓库根目录名仍是 `HarnessPRD`（勿改）；远端 `origin = github.com/Jenlybein/PRDwright`，CI 只有 push 后才真跑。
- 本地 curl 一律加 `--noproxy '*'`；Vite 8 只监听 IPv6 `[::1]`；删 schema 后复查 import 孤儿。
- 临时预览页用 `frontend/*.html` + `src/__preview__/*.ts`（样式入口 `src/index.css`，不是 `assets/`），核对完删干净；别用内联 `template` 写预览入口（runtime-only 构建静默不渲染），用 `h()`；截图前调 `__VUE_DEVTOOLS_TOGGLE_OVERLAY__(false)`。

## 2. 后端分层与数据层

- `api/` 只解析 / 校验 / 封装 → `services/` 业务 → `repositories/`（唯一碰模型与查询的地方）。
- Pydantic 模型集中 `api/schemas/`（按领域拆成 9 个子模块，`__init__.py` 是唯一出口），被引用的模型必须先定义；Router 各自带完整 prefix，`main.py` 不叠加。
- 仓储返回模型对象或 frozen dataclass 投影，不返回裸 dict。
- 仓库是绑定 `Session` 的实例方法：不许 `import sqlite3`、不许 `.commit()` / `.rollback()`、不许 SQL 字符串字面量（守卫 C1/C2/C3）。
- 事务边界唯一落点是 `session_scope()`（正常提交 / 异常回滚 / 必定关闭）：仓库只管翻译，服务层负责「一个业务操作 = 一个事务」；不许在 `async def` 里跨 `await` 持有（C4）。
- 引擎参数固定 `expire_on_commit=False`（防 `DetachedInstanceError`）+ `autoflush=False`（要自增 id 就自己 `flush()`）。
- `max_overflow=0` 是承重设计：运行期一条连接都不许新建（躲 T15 的 WAL `-shm` `SQLITE_READONLY` 竞态）；启动路径必须在 `prepare_schema` 之后调 `warmup(engine)`。
- 数据层是 SQLModel：时间工具在 `core/clock.py`，引擎 / 事务在 `core/engine.py`，连哪个库在 `core/database.py`。
- 列类型只走 `core/types.py`：`short_text(n)`（可索引）/ `long_text()`（大段文本）/ `timestamp_text()`（=32）；`models.py` import 时 `assert_metadata_is_portable` 自检，违反即抛错（v8）。
- 大段文本上限 60KB（字节），闸门在 `core/limits.py::ensure_text_within_limit`，接在 `append_version` 与 `persist_progress/finish/fail`，超限抛 `INVALID_REQUEST`（v8）。
- upsert 只走 `core/dialect.py`：`upsert_values` / `upsert_select`，仓库里不许再 import `sqlalchemy.dialects.*`；MySQL 的 DO NOTHING 用主键 no-op，绝不用 `INSERT IGNORE`（会吞列超长 / NOT NULL）；只准用在「该表只有一个唯一约束」的表上（契约 C-D1，v8）。
- SQLite 与 MySQL 的差异收在 `core/dialect.py`：四模块边界 = `core/database.py`（连哪个库）/ `dialect.py`（语句）/ `types.py`（列声明）/ `schema.py`（迁移），可移植性台账见 `docs/architecture/v8/ARCHITECTURE.md 附录 B`。**唯一部署后端是 SQLite**；MySQL 相关分支是遗留代码（未验收、不再扩展），不计划引入其它后端。改动遗留 MySQL 分支时仍守三条——`TEXT` 列不能有字面量 `server_default`、索引列必须 `short_text(n)`（n≤768）、死锁要重试整个事务。
- `get_settings()` 走 lru_cache：改环境变量后必须 `cache_clear()`；`prompts.py` 走 `str.format()`，字面量花括号写 `{{ }}`，给 LLM 的指令用 `<!-- -->` 注释。

## 3. 迁移（Alembic）

- schema 只走 Alembic：新表 / 新列写 `backend/migrations/versions/`，入口 `core/schema.py::prepare_schema`，迁移在 `lifespan` 里跑（重启 = 跑迁移）。
- v20 之前的旧库拒绝启动并打印逐条差异；不要再写兼容垫片。
- 迁移手写：autogenerate 会生成 `drop_constraint(None)`，在 SQLite 上必失败。
- batch 迁移会丢 `sqlite_autoincrement`（反射读不到表级参数，重建后 id 开始复用）→ `batch_alter_table(..., table_kwargs={"sqlite_autoincrement": True})`；自增表全集在 `0002_typed_columns.py::_AUTOINCREMENT_TABLES`。
- 删列只用原生 `ALTER TABLE … DROP COLUMN`：`PRAGMA foreign_keys` 在事务内是空操作，「关外键 + 重建表 + 改回」那套在步骤内关不掉。
- 删列 / 退役列是单向门：删完旧代码读不到该列，回滚只能靠备份库文件，不设兼容期。
- 迁移用例别写死「第几步」：真假源是 `alembic_version`，新迁移比对 head 而不是抄字面量。
- 守卫 C9 = 旧迁移链不许回来（文件存在性 + AST，docstring 豁免）。

## 4. LLM 与 SSE

- 只经 `llm_factory.get_llm()`；默认 ark + `doubao-seed-2-1-turbo-260628`（该模型不收 `thinking.type:"auto"`，只认 `disabled`）。
- 流式唯一入口 `stream_reply.py`；出错只发 `error` 不发 `done`；`on_finish` 仅成功时调、且在 `done` 之前。
- 区分 `stage_chunk` 与 `chunk`：前者是当前阶段的原始输出（审核意见 / 改写稿），只进阶段输出面板；混用会把审核 JSON 写进正文。
- 下游文档开流首条必须 `stage(writing)`；阶段输出归 `JobRuntime`、换阶段即清空、不落库；重连恢复正文必须 `finalized_text + live_buffer`。
- 流式工具调用必须逐片相加（`AIMessageChunk.__add__`）：整批塞进一个 chunk 会拆成「空调用 + 废调用」，症状是「模型调了工具、选项卡一张不弹」；假模型必须复现真实分片形态。
- token 计量唯一落点 `astream_text`；方舟 `include_usage` 的用量帧 `choices` 为空 → 先读 `usage`，别 `continue`。
- 预算检查在 `astream_text` 开头、记账在末尾；超顶复用 `INVALID_REQUEST`（别标 `LLM_UPSTREAM_ERROR`，否则前端白重试）；`max_retries` 设 0；`text_emitted` 守卫：yield 过增量就绝不重来。
- 需整段结果（JSON 回填等）用 `document_service._collect_reply()`（拼增量 + 截断检查）。
- SSE 分帧唯一实现 `api/sse.py::make_sse_generator`（已带 `X-Accel-Buffering: no`）；事件 `status/chunk/part/stage/review/done/error` + 末尾 `data: [DONE]`；服务层与路由层两层异常兜底；前端用原生 `fetch` + `ReadableStream`（axios 不支持流式读）。
- 心跳 `: ping` 是 v7 必做，只能走 `api/sse.py` 这个出口。
- 三家 LLM 抽象、路由 / service 分离、SSE 两层兜底、技能包驱动 + 资源缺失降级：别动。

## 5. 生成与审核管线

- `PRD_PIPELINE_CONCURRENCY=1` 是串行回滚路径（不发 `review`、不产生 `partial`）；`>1` 并行（生成车道严格按章序、每章派生审核 + 改写任务、一个信号量罩住全部 LLM 调用）。并行牺牲跨章一致性，`review` 只聚合各章结论。
- 两条轨：`chunk` 是过程字（可能缺标题）、`part` 是权威正文（整段覆盖该章）；分章 `chunk` 必须带 `section_id` + `index`。
- `part` 带 `index`、可乱序：看板装配与 `done.reply` 一律按 index 排序；并行必须关闭逐字 `chunk`（已交付的增量撤不回来）。
- `stage(writing)` 放在单章重试循环内；按章分缓冲 `live_by_index`；`part` 只清该章；`_draft` 按 index 交错归并。
- 失败语义：单章每阶段按 `PRD_SECTION_MAX_RETRIES` 重试；撰写用尽写占位继续、仍 `completed` 且 `partial=true`，审核用尽保留正文，改写用尽保留改写前版本，全部章节失败才 `error`。
- `review_enabled=False` 时连 `review` 事件都不发（报「0 个待处理问题」会被读成「审过了、没问题」）。
- `review_enabled` 对三份产物都生效：PRD 关逐章审核，接口 / 提示词关「整篇审 + 改写」；默认口径不同——PRD 取 `PRD_SECTION_REVIEW_ENABLED`，另两份 `None` 视为开（只有 `False` 才是关）。
- 只有 `high` 触发改写（`REVIEW_REWRITE_MIN_SEVERITY`，两链共用）；`verdict` 如实报、只是不动笔；改写后不复审。
- 审核写批注、不复述正文：problem 条目带 `where`（标题路径，取值域由提示词枚举的章节清单限定）、禁止摘录原文、`detail` 限长（解析时截断）；severity 是改写门槛唯一输入，不设 `low`。
- 审核的 max_tokens 只能靠换实例生效（`astream(..., max_tokens=)` 不生效）：落点是 `DocumentService.review_llm` / `PrdSectionWriter.review_llm` 两个惰性 property，都在后台配置页、不在 `.env`。
- 整篇链路分节改写：只重写 `where` 命中的节、按标题拼回；`part{replace}` 必须发拼好的完整正文（前端无条件整段覆盖）；两条兜底退回整篇重写——`where` 一条都定位不到、拼回后短于 `_MIN_KEPT_RATIO`；`DOC_SECTION_REWRITE_ENABLED` 一键回退。
- 只改 N 节时，未被改写章节的待确认项必须合并保留（`pending_items.finalize_items`）。
- 骨架化 `doc_skeleton.py`（纯字符串、零 LLM 调用）只用在审核侧：标题整行 / 表格整行（只留首列会丢接口路径）/ 条目整行 / 含数字行 / 代码块只留围栏首行；撰写侧的需求输入不动（唯一事实来源），撰写侧要带 `format_field_contract`（枚举递归扫）；`skeletonize` 喂 fenced JSON 会静默吐空。
- PRD 改写不复用 `PRD_SECTION_GENERATION_PROMPT`（6000 字符字段契约 + `{{占位符}}` 会诱导重新渲染），改用 `PRD_SECTION_REWRITE_PROMPT`。
- 待确认清单唯一实现 `services/pending_items.py`：`extract()` 剥离全部块并按 `{prefix}-{n}` 重分配 id、坏 JSON 与未闭合块跳过；`id` 由后端分配、不信任模型的 id；抑制器流结束仍在块内则什么都不输出。
- 清单三路可达前端：`done.data.pending_items` → `snapshot`（`generation_jobs.pending_items` 列，`_snapshot_event` 与 `_db_snapshot_event` 两条路都要带）→ `GET .../documents`。
- 澄清档案 `doc-clarification` 的 `opening_fields` 必须与模板占位符一一对应（多一个 KeyError、少一个静默填空）；`continue-stream` 也要带 `profile_key`，且 `profile_key` 校验必须在构造 SSE 之前。
- 澄清改写走 `clarify` 任务（复用 job 框架，不是裸 SSE）→ `apply_clarification_stream`；「只动涉及章节」只能靠提示词约束（代码保证不了），提示词里写「逐字保留」。
- 技能包：归属与资源由 `skill.yaml` 自述（`applies_to` / `references[]`），加包 = 加目录、别加中心注册表；唯一入口 `load_skill_artifacts(kind)`，缺件返回 `None` 降级且降级要在回执上看得见；规范整段注入、不检索，改写 / 澄清按白名单 `format_rewrite_reference`。
- `prompts` 生成线的占位符（`prd_content` / `api_docs_content` / `pending_spec`）是跨文件契约：技能包版与 `prompts.py` 兜底版少一个不是「那节空着」而是 `KeyError`；留空填 `NO_API_DOCS_NOTE`。
- `optimize` / `clarify` / `auto-clarify` 的 `kind` 只说「做了什么」，目标产物取 `doc_kind`（回执与落库共用 `_target_product_kind()`）；资源存在性不进缓存。
- 交付：`GET /api/solutions/{id}/export` → ZIP = 三份 md + `README.md`；只导用户看得见的；目录名必须消毒（`_safe_name` 抹 `..`、拦 `con` / `nul`）；上限 50MB、CSV 带 BOM。
- 上游正文变化（含手改，按 `content_sha`）只让下游 `stale=true` 亮黄条：不删除、仍可交付、只汇总提示不硬拦。
- 接口 / 提示词：整篇生成 → 审一次 → 有问题改写一次；`review` 结论不持久化（刷新即失），展示在「待确认问题」之下、正文之上（`ReviewConclusionPanel`）。
- 澄清提问上限 1-2 个，两处落点必须同步改：后端 `conversation_service.REQUIREMENTS_WRAPPER`（首轮开场白，是最后一条 user 消息）+ 前端 `useClarificationChat.ts::CLARIFICATION_SYSTEM_PROMPT`（开场白更靠后更具体、会盖过系统提示词）；口径由 `test_conversation_prompt.py` 锁死。
- MVP 留空时按 `ui_pages` 推断候选功能清单，逐条在功能点名称后加「（建议）」，优先级 / 验收写「待确认」，`ui_pages` 也为空才退回「待确认」；改这条要同时改 5 处（`writing-rules.md §4`、`prd-template.md 2.1/6.1`、`SKILL.md` 阶段一/四、`field-schema.json` 的 `mvp_features.description`、`prd_section_writer.py` 的 `features` / `scope` / `pending` 三处 `focus`）。
- 未填的题由 `format_form_data` 在已答项后汇总一行「以下题目用户未填写：…」，空串与「键不存在」同等看待；配置读不出来记 warning（降级要看得见）。

## 6. 前端与状态机

- `main.ts` 只挂 `AppV2`；router 只有 `/v2`、`/v2/:id`，阶段流转走状态机、不进 URL；外壳在左侧 `SolutionSidebar.vue`（无顶栏）。
- `types/index.ts` 是契约准绳（不一致时改后端迁就前端）；状态在 Pinia `stores/{workflow,solutions,auth}.ts`；Markdown 统一走 `utils/markdown.ts::renderMarkdown()`（`html:false` 防 XSS）。
- 入口 = 一屏两组四叶子（`EntryStage.vue`）：`从零创建 PRD`（`structured` / `prd-quick`）｜`导入已有素材`（`prompt-debug` / `prd-and-api-paste`）；`prompt-debug` 键名故意不改，新键刻意不叫 `prompts`（那是 `DocKind` 取值）。
- 流程：入口 → 表单 → 对话 → 闸门①摘要 Diff（回填失败返回原摘要深拷贝）→ PRD → 接口文档 → 提示词 → 交付；文档澄清就在对话页。
- 加一个入口模式要同步十来处（`EntryMode` / `ViewState`、`VIEW_STATES` / `isEntryMode` / `MODE_STEP_IDS` / `REQUIRED_DOCS`、`MODE_LANDING` 与「快照四件事」+ `hasContent` + return、`MODE_LABELS` / `STAGE_LABELS` / `computeTitle`、`AppV2.vue` 渲染分支、`backToInput` / `backLabel`、`AppV2.spec.ts` 的入口文案断言）：漏一处大多不报错、只是行为不对。
- 加 `ViewState` 同步四处：`ViewState` / `VIEW_STATES` / `STEP_OF_STATE` / `STATE_DOWNGRADE`，漏一处只表现为进度条算错或刷新后进不去。
- 快捷流程 = 同一管线、六步齐全：`REQUIRED_DOCS` 与 `structured` 一致、`STEP_OF_STATE['prd-quick']='form'`；`hasRequirementChat()` 只认 `structured`；「AI 对话」那格不能省（找不到就归零 → 停在对话页却高亮「描述产品」）；它不问问题，缺的信息靠「待确认」+ 审阅页「AI 自行澄清」。
- 进度条「AI 对话」摘到最左、用竖线隔开（纯呈现，`stepsForMode` / `stepIndexOf` / `reachedStepIndex` 未动）：高亮与可点一律按「原始下标」；分隔线用 `border-r`、别插 div；`li` 数量仍为 5。
- `DocKind` 含 `optimize` / `clarify` / `auto-clarify` / `chat`（动作），`DocumentKind` 只三份（东西），`isDocKind` 必须认 `chat`；前端 `api-docs`（连字符）/ 后端 `api_docs`（下划线），唯一转换点在 `documentService.ts` 与 `api.ts`。
- 四条澄清线程（需求 / PRD / 接口 / 提示词）全在同一对话页：单 `messages` + 每条打 `stage`，只渲染当前标签那一条；`chatTab`（意图）与 `activeChatTab`（实际显示）必须分开、后者绝不回写；回填只取 `requirement`；`ProgressSteps.canClick` 必须含「当前步骤」；线程作废只在整篇重生成。
- 澄清改写 / AI 自行澄清切到 `generating-<kind>`，完成回审阅；`goTo` 必须在 `job.start` 之前（`docGenerating` 置位后 busy 守卫会拦），并把 `clarifyTarget` 钉为 `'prd'`；改写失败也留在生成视图。
- 澄清上限与分级 = 后端共享常量 `CLARIFICATION_QUESTION_RULE`，各档案整段引用、别各写数字；等级没有兜底（`IMPORTANCE_ORDER` 是 P0..P6，缺失或非法一律丢弃）；未注册档案名 → 400。
- 澄清选项卡挂 `ChatMessage.asks`（不加顶层快照字段）；无选项时不写 `asks` 字段（恒带 `[]` 会撞破形状断言）；选项说明不许空；卡片里不放输入框；多选答案按选项序拼、不按点选序；写回会话历史的必须是剥离后的干净正文；可交互判据 =「当前线程最后一条消息」。
- `stores/runs.ts` 是「有没有任务在跑」的唯一真源，三层判据 `jobId`·`sessionEpoch`·`docKind`，不许猜默认值；`goTo()` 不因「忙」拒绝跳转、入口切换仍拦；`runs.begin` 不清预览（只 `resetDocTransient()` 清）；`applyJobSnapshot` 的 PRD 守卫按「看板有没有卡片」判，`onChunk` 参数是对象 `{text,sectionId,index}`、整体转发。
- 重连种类三段兜底：`pointer.jobKind` → `activeJobKind` → `kindFromViewState()`；拿到快照按 `snap.kind` 复核，不符就 `return`。
- 切方案先 `detachAllSubscriptions()+runs.detach()`、再 `sessionEpoch += 1`；主动退订 ≠ 失败（`AbortError` → `aborted`）。
- 任务与连接解耦：`POST /api/jobs` + `GET /{id}/stream`，`detach()` 只退订不取消；同一任务绝不能并发订阅两次（两路 SSE 交错写同一份预览缓冲 → 整章重复 + 表格单元格成对重复，而只有预览坏、落稿是好的）；两道锁：`loadAndResume` 按方案 id 单飞（`main.ts` 未等 `router.isReady()` 就挂载）+ `reconnectJob` 复位前再单飞。
- 预览必须幂等：`part` 按 `sectionId` 去重，`applyJobSnapshot` 只在预览还空着时初始化；`optimize` / `clarify` 不切阶段，重连落点由 `docKeyForJob(kind)` 定。
- 顶部提示一律走 `AppV2.vue` 浮层（`fixed right-4 top-4 z-50 w-72`），绝不进文档流；自动保存成功 / 进行中都静默，只有失败弹（`saveError` 的 watch 要覆盖任意失败）。
- `ui/AppActionBar.vue` 是全站唯一操作落点：最多两行、主操作永远最右且唯一、吸底 `fixed bottom-0`（不要 `sticky`）、没有「保存」按钮，卡片内嵌底栏传 `:sticky="false"`；底栏高度写进 `--shell-action-bar-h`，只允许「可见的那条」写（`el.getClientRects().length`，`v-show` 隐藏的 0 会覆盖可见值），首次同步放 `requestAnimationFrame`。
- `DocumentReview.vue` 只渲染卡片、不自带控件，工具全在底栏、经 `defineExpose` 交出；「保存」判据是「有正文」不是「改过」。
- 底栏读子组件句柄要兜 null（模板 ref 首帧后才赋值），断言状态前必须 `await nextTick()`。
- 待确认清单面板在正文之上（`PendingItemsPanel.vue`，只 `ReviewStage` 用），默认铺开 2 条、其余可展开；折叠只改铺开数量、不动排序、不进快照；序号用位置下标，后端 `id` 只作渲染 key。
- 主题三层令牌（`@theme` → `@theme inline` → `:root` 亮 / `.dark` 暗）：`inline` 必须保留、`:root` 必须写在 `.dark` 之前；只用语义令牌、禁用未定义令牌类（Tailwind 不生成 → 回落 currentColor）；`dark:` 只留给结构性判断、状态色 tint、模态遮罩。
- 改主题只动 `index.css` + `useTheme.ts` + `ui/ThemeMenu.vue` + `index.html` 首帧脚本；前端不存界面状态，唯一 localStorage 键是主题的 `theme`（两处共用键与解析规则）。
- 表单 `RequirementsForm.vue`：不让用户填格式、行输入用 `:value` + `@input`（数组下标 v-model 报类型错）、元素 id = 错误键、折叠用 `v-show` 保留 DOM、默认只展开「产品与用户」；MVP 功能选填（整行留空的行过滤 + 跳过校验）。
- 「默认该开」的布尔 prop 必须 `withDefaults(..., { x: true })`（Vue 会把未传的布尔 prop 转成 `false`，完整流程被悄悄降级）。

## 7. 持久化与版本链

- 三份正文只在后端；正文只住 `document_versions`（`documents` 只剩身份 + `current_version_id` 指针），读当前一律 `LEFT JOIN`。
- 写入语义：整篇生成 = 追加新版本（不再删下游）；`optimize` / `clarify` / 手改 = 就地改当前版本；checkpoint / restore = 追加新版本，恢复绝不把指针拨回去（历史只增不改）。
- 「是否追加版本」与「是否删下游」恒等（都由「是不是整篇重生成」决定），`job_service` 收成一个 `append_version` 标志。
- 环形外键：先空指针插 `documents` 行 → 插版本 → 回填指针（`_ensure_document()`）。
- 写正文唯一入口 = job 成功收尾（`_write_document` → `commit_generated`）：正文 + 清单 + 回执同批写入、失败不覆写；新增产出正文的路径不要自己写库，接进 `_build_event_stream` 的 `done`。
- `save_content`（手改）与 `commit_generated`（整批写入）别互相顶替；`optimize` / `clarify` 落地靠请求体 `doc_kind`，取不到 → 不落库也不猜默认值。
- 版本路由挂 solution 作用域（`.../{kind}/versions`、`.../versions/{vid}`、`POST .../{kind}/checkpoint`、`POST .../{kind}/restore`）；版本不存在复用 `SOLUTION_NOT_FOUND`(404) 不新增错误码：「方案不可见」404、「还没有任何版本」200 + 空列表。
- 删除只删历史版本，当前版本回 400 `INVALID_REQUEST`；守卫写在仓储 SQL 的 `WHERE`（`v.id != COALESCE(d.current_version_id, -1)`），不是「先查后删」。
- 删除只重拉列表、不重拉当前文档；checkpoint / restore 相反（要 `documents.reload()` + 重拉列表）。
- `fetchVersion` / `deleteVersion` 的 404 不收敛成 `DocumentsNotFoundError`，直接透传后端 `detail`（`fetchVersions` 仍走它）；`DocumentVersionDetail` 不含 `contentSha` / `contentLength`，别用 `...toVersionSummary()` 展开构造。
- 前端另开 `stores/documentVersions.ts`（别挂 `documents`），抽屉 `ui/VersionDrawer.vue` 纯展示、点条目才 `fetchVersion` 单取详情并就地加宽到 640px；写动作先 `flushPendingSave`，成功后 `documents.reload()` + 重拉列表；删除与恢复都要二次确认且互斥，Esc 逐层退出（先撤确认 → 再退预览 → 最后关抽屉）。
- 底栏唯一操作落点的有意例外：抽屉只放内容操作（存为新版本 / 恢复 / 删除），流程动作永远只在底栏；抽屉以 `--shell-action-bar-h` 作下边距不遮底栏。
- 保存单飞，唯一入口 `scheduleSave()`；`changeTick`（该存）≠ `restoreTick`（重建组件），用 restoreTick 触发保存会把刚读回的存回去还刷 `updated_at`。
- 两根时间轴：列表时间与排序一律用 `content_updated_at`；推进判据 `content_changed` = `workflow.contentTick` ∪ `documents.contentWriteTick`（分两个计数是防成环）。
- `content_changed` 缺省假，且只在保存成功之后把基准推到发送时的值（失败不能推进，否则静默丢一次变更）；打开方案时要同步两个基准。
- 新增「会改作品」的路径都要在写成功后调 `documents.markContentWritten()`，漏了只表现为「列表时间不动」。
- `stores/solutions.ts` 是策略中心：防抖 1.5s、`generating-*` 不存、在途变化只记标记回来补存；标题取 `summary.product_name` → `quickInput.productName` → `debugPrdText` 首个「# 」→ `未命名方案 · MM-DD HH:mm`。
- 列表 `stage` 是纯展示冗余列、写最远里程碑，别退回 `viewState`（回看旧文档会让进度倒退）；`mergeIntoList(summary, toTop = false)` 原位替换、只有新建才插最前（判据用显式参数，不靠「id 在不在列表里」）。
- 侧栏切方案必须走 `router.push('/v2/{id}')`；`openSolution()` / `startNew()` 开头先 `flushPendingSave()`。
- `stores/documents.ts` 是三份正文的前端唯一工作副本：防抖 800ms + 单飞、`loadAll` 有序号守卫、未落库的手改优先于响应；手改落库触发点是 `DocumentReview` 的 `draftChange`（`generating` 时不报、`draft === props.content` 时不报，别把 props 的 watch 挪到后面）。
- 冲盘三处：`solutions.openSolution`（必须在拉文档之前）、`beforeunload`、`onBeforeUnmount`；`goTo` 刻意不冲。
- 打开方案是异步恢复，`RequirementsForm` 只在 setup 读一次 `:initial` → 靠 `restoreTick` 顶掉 `formKey` 强制重建；不要改成 `watch(props.initial)` 回填（AppV2 测试会死循环）。
- `SessionDataV2` 不再有 `*Content` / `*RunSummary`；新增字段要同步 snapshot / applySnapshot / reset / watch 四处。
- 越权 / 不存在一律 404（不是 403）；`SolutionRepository` 强制 `owner_id` 首参且不接受 `None`。

## 8. 日志 · 密钥 · 配置

- 日志唯一入口 `configure_logging`；`request_id` 由格式化器从 contextvar 现取，不要手写；业务字段丢 `extra=`；测试前后必须 `reset_logging()`。
- 日志双写：轮转文件（永开）+ `app_logs`（可关）；`LogStoreHandler` 三命脉——队列满就丢、失败只降级 WARNING 绝不抛、本模块日志不入队；`formatException` 属 `Formatter`；线程等队列切 ≤50ms；时间统一 `utc_ms_text`（毫秒 + `Z`）。
- 密钥加密入库：`backend/secrets/master.key`（600）；空 `value_token` = 未配置（本地模型填 `not-needed`）；面板只回 `key_present`。
- 轮换先重加密再换主密钥（`generate_keys.py --rotate`），主密钥保留每一把。
- 热配置读 `get_runtime_config().settings`，`get_settings()` 只反映环境变量；单例里不缓存可热改的值；`WHITELIST` 含 `auth_cookie_secure`（热改下一次登录生效）。
- `/health` 恒 200（status 只 `ok` / `degraded`），别拿它当存活探针；三项、供应商只校验配置；llm 项对「主密钥解不开」返回 `False` 而非抛错，症状与「后台没填密钥」一模一样 → 排障先查 `PRDWRIGHT_MASTER_KEY_PATH`。
- 配置项控件类型按 `Settings` 注解推导；Vue 模板 / `help` 里不能用 markdown 的 `**`。
- 登录链路是同步代码（`time.sleep` 卡死事件循环）→ 延时走 `api/login_flow.py::_sleep`；失败渐进延时 2/4/8 秒，不存在的用户名也计数。
- 不上 JWT（触发条件是多实例不共享会话存储 / 微服务传身份 / 给第三方发 token）：随机 token 存库 + HttpOnly Cookie，`TokenIssuer` 封装以便替换；密码 `hashlib.scrypt` + 独立 salt；注册开放但需邀请码（`invite_codes.max_uses` 0 = 不限，首启播种 `.env` 的 `INVITE_CODE`）。
- 单点登录：`replace_for_user` 在一条事务里先删该用户全部旧会话再插入；刻意不按过期时间挑着删；残余风险 = 多设备互相踢下线。
- 门禁 = 中间件默认拦截 + 白名单（只有 `/`、`/health`、`/api/auth/*`、`/api/conversation/questions`）；不变量测试遍历 OpenAPI 断言非白名单必 401；中间件必须是纯 ASGI 类（`BaseHTTPMiddleware` 会重包响应体、对 SSE 有风险）；`AUTH_ENABLED` 默认 true；接口前缀 `/api/solutions/*`，运维 CLI 是 `backend/scripts/auth_cli.py`。
- 未登录禁止创建任务：可浏览 `/v2` 入口页，点入口即弹 `AuthDialog` 且不进入流程；登录成功只关弹窗、回入口页让用户重选；未登录时 `viewState` 锁在 `entry`。

## 9. 管理端 frontend_admin

- 独立工程，端口 5174，与 `frontend/` 零共享；`npm install` 要 `--legacy-peer-deps`。
- 门禁只是体验，真边界是后端 `require_admin`；判管理员 = `/api/admin/me` 状态码（200 / 403 / 401）；会话阶段 `unknown|anonymous|admin`，unknown 不能猜；管理面查询跨 owner（JOIN users），边界只在路由 `require_admin`。
- 后台登录 `POST /api/admin/login`；规则只在服务层写一份；「撤销自己」不算违规（删号 / 停用自己仍 403）；前端不硬编码规则，原样显示 `detail`。
- 审计表 `admin_audit_log` 不加外键、不写密码。
- 视觉 Sky Brutalism：令牌与 `.brut-*` 全在 `index.css`；天蓝只表「强调」不表「结构」；页头统一 `PageHeader.vue`（`.page-head` 实色带 + 3px 下沿线，无投影），说明文字进 `#desc`。
- 需额外空间的交互一律走 `AdminModal.vue`；弹窗开关与「提交中」是两个变量；行内动作 >3 个收进「操作 ▾」。
- 主题切换 = 单按钮循环，权威在 `useTheme.ts::cycle()`；`localStorage.theme` 与 `index.html` 内联脚本共用键、解析规则一起改。
- 视图用例靠文本与结构定位——新增控件别排在被断言的那个前面。
- 「卡死」= running 且 `updated_at` 30 分钟未更新；`is_stuck` 只由服务端算；自动刷新 10s 轮询不用 SSE。
- 级联：`users` → 会话 / 方案（→ 文档 → 版本 / 线程）/ 任务；有意不级联 `admin_audit_log` / `app_logs` / 全局配置；删号必须 `clear_login_failures()`。
- 删除前 `cancel_for_user()` 停在飞任务（只改库不改内存 → 协程照旧烧 token）；删方案走 `cancel_running` 落 `failed + CANCELLED` + `_finalize` 兜底（`generation_jobs.solution_id` 无外键）；二次确认要求手打用户名。

## 10. 测试

- 基线 **1089 例**（本分支工作区，pytest 收集数）；注意「`def test_` 文本计数 = 955」与「收集数」不是同一个数——差额 134 来自 parametrize 展开。默认 `addopts = "-n auto"`，全量 ~100 秒；单文件调试加 `-n0`。旧口径里的 975 / 863 / 867 / 999 / 1006 / 1014 全是过时数字，别再用（报基线前先 `uv run pytest --collect-only -q` 现取）。
- 退出码不可信时用 `uv run pytest --junit-xml=...` 判成败（沙箱可能拦 `%TEMP%`；必要时加 `--basetemp=<项目外可写目录>`）。
- 两个人同时跑全量会互相污染（单条从 0.5s 涨到 3s+，`LogStoreHandler.close()` 超时后写进下一用例的库 → `total != 25` 偶发红）。
- 别在 `test_admin_logs.py` 断「全表行数」：每请求都异步写一条 `request.completed`，与 `total==N` 赛跑（单文件绿、全量红）。
- 测试直连库用 `tests/dbsupport.py::connect`；建库是「每进程模板库 + 每用例拷贝」（`_schema_template` 走真实 `prepare_schema`），别改回每用例跑 alembic 或 `create_all`。
- `--durations` 输出降序，要看头部（`tail` 看到的是第 30 名）；真实长尾是 `llm_retry_delay` 1.5s × 重试，并行压不掉。
- 前端：`npm run build` = type-check + build（`vue-tsc` 含测试文件）；`test:unit` 底层是 vitest，CI 必须加 `--run`；全流程离线、不需要任何 LLM Key；改前端后必须跑一次后端用例，改契约 / `ViewState` 时先改测试夹具。
- 前端测 AppV2 必须有真 router（`createMemoryHistory`）且 `await router.push(path)` 之后再 mount；弹窗 Teleport 到 body 要查 `document.body`；表单提交用 `dispatchEvent(new Event('submit'))`；编排测试不要用「填充示例」准备数据；`clickButton` 默认「包含」匹配，入口用 exact；`mountExistingSolution(snapshot, id, documents)` 的第三个参数会覆盖 `fetchDocuments` 的 mock。
- `it` 里改共享 mock 会一直生效到后面所有用例；`vi.clearAllMocks()` 清不掉普通数组；验证用例真能抓 bug = 临时删那行跑红再还原；jsdom 下 `isVisible()` 对 `v-show` 不可信 → 用 `v-if` + `exists()`。
- 组件测试取徽标用 `li > span[aria-hidden="true"]`（章节与拍板人之间的「·」也有 `aria-hidden`）；焦点类断言必须 `attachTo: document.body`；`noUncheckedIndexedAccess` 下 `arr[0]` 是 `T | undefined`，测试里用命名常量。
- 读 docstring 不足以确认运行期行为：要顺着真实启动路径核（`main.py` 的 lifespan 走 `prepare_schema`；conftest 走 `dbsupport`）。
- 给一个性质写用例时要构造能证伪它的输入（只按正序点击的「行内顺序」用例，名字声称的性质从没被守住）。

## 11. 部署

- 形态 = 单域名 + 路径前缀（`/` `/admin/` `/api/`）：Nginx 唯一对外，后端只听 `127.0.0.1:8000`；否决子域的硬理由是 Cookie `host-only`（`set_cookie` 无 `domain`）；`CORS_ORIGINS` 设空串（同源不需要）。
- 单 worker 是硬约束：`_RUNTIMES` / `JobBus` / runtime 快照 / 连接池都在进程内，不引 gunicorn；发布前排空（`/api/admin/jobs?status=running`）；重启 = 跑迁移。
- 四目录：`/opt/prdwright`（代码，必须含 `backend/migrations/` 与 `skills/`）· `/var/lib/prdwright`（DB）· `/var/www/prdwright/releases/<ts>` + `current` 符号链接 · `/var/log/prdwright` + `/etc/prdwright/{backend.env,master.key}`（600）。
- 发布顺序「先备份 → 换代码 → 重启（迁移自动跑）」；回滚 = 恢复备份（不是反向迁移）；备份没做成不许重启。
- 恢复后第一次启动会再跑 `upgrade head`：备份的 revision 比代码新则启动失败 → 演练必须用当前代码。
- 备份用 `VACUUM INTO`，不能 `cp`（WAL 会静默缺最近写入）；追加自检（`integrity_check` + 读 `alembic_version` + sha256）；主密钥保留每一把。
- `PRDWRIGHT_MASTER_KEY_PATH` 走 `os.environ` → `.env` 读不到，必须 `EnvironmentFile`；`PRDWRIGHT_SKILLS_DIR` 走 `lookup_env`（两条路都通）。
- 环境硬事实：`requires-python >=3.12`（Ubuntu 22.04 不够，建议 24.04）、Node `^22.18 || >=24.12`、无 requirements.txt（`uv sync --frozen`）。
- 管理端挂 `/admin/` 要两处一致：`vite base: '/admin/'` + `createWebHistory(import.meta.env.BASE_URL)`（Vue Router 的 `normalizeBase()` 不跟随 Vite 的 base）。
- Nginx：SSE 三行（`proxy_buffering off` / `read_timeout 1800s` / `gzip off`）+ 服务端 `: ping` 心跳；`assets/*` 长缓存、`index.html` 必须 `no-store`；`/docs` 生产 404（后端不注册 + 白名单移除 + Nginx 兜底）。
- `deploy/` 目录是 v9 的交付物（compose、反向代理、env 样例、启动脚本、备份脚本、README），**已随 v9 落盘**：`ls deploy` 与 `ls deploy/docker` 都看得见，交付形态的唯一说明在 `docs/architecture/v9/ARCHITECTURE.md`。v7 文档里那几份（`backend.env.example` / `systemd/prdwright.service` / `nginx/prdwright.conf` / `scripts/deploy/{backup,restore,release,smoke}.py`）**从未落盘**，只作当期的形态记录，别照那些路径找文件。

## 已失效（勿再引用）

- `core/db.py` 与那个 444 行连接池实现的 `core/database.py`：已删（v6 换 SQLModel）；旧说法「`core/database.py` 已删」已作废 —— v8 里该文件名以「连哪个库」的四模块之一重新存在。
- `SCHEMA_SQL` / `init_schema` / `PRAGMA user_version` 的 20 步手写迁移链（`core/migrations.py` 的 `MIGRATIONS` 清单）：v6.1 整体删除，被 Alembic 取代（`core/schema.py::prepare_schema`）；「自研迁移比 alembic 省」的论证同时作废。
- `solutions.data` 存全量 `SessionDataV2` JSON（后端不解析 data）：`data` 已退役（create 写空串、update 不碰），正文住 `document_versions`。
- 三份正文存 `documents` 的正文列：v6 删列（单向门），被 `document_versions` 取代。
- 闸门② RAG（`retrieve-api-docs-rag` + `core/api_doc_knowledge.json` 本地词法检索 + `RagResultPanel`）：已整条移除。
- 整篇生成删下游、`_write_document` 一律传 `invalidate_downstream=True`：已被「下游只置 `stale` 亮黄条、不删、仍可交付」取代。
- `quality_gate` / 需求基线签字 / `manifest.json`：裁决不做（`quality_gate` 恒 `'{}'` 的旧口径一并作废）。
- 三家 LLM 自动 failover：已取消，不再跨供应商换线。
- `PRD_SECTION_REWRITE_APPENDIX`：已退役（改写改用 `PRD_SECTION_REWRITE_PROMPT`）。
- `clarification_ask.render_ask_spec()` 这个旧名：已退役，现名 `render_ask_tool_instruction()`。
- 测试建库用 `init_schema()` 或每用例 `create_all`：被「`tests/dbsupport.py` 模板库 + 每用例拷贝」取代。
- 用例数基线 93 / 217、477 / 588、863 / 867、975、999、1006、1014 / 986：全部作废，现行基线是 **1089（本分支工作区，pytest 收集数）**。
- 排期真源 `docs/architecture/v3/roadmap.md`：只是 v3 期的口径，别当现状真源（现行排期看 `docs/architecture/v9/roadmap.md`；决策与取舍看各层 `ARCHITECTURE.md`）。
- `docs/architecture/v*/` 下原有的按层决策记录文件与 `docs/architecture/evolution-truth.md`：**文件已删除**（决策记录目录随后也整体移除），`evolution-truth.md` 的独有内容并入 `docs/architecture/README.md`；按旧路径引用一律无效。
