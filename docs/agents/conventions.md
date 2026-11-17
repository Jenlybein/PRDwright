# PRDwright 工程约定（agent 必读）

> 一句话说明：这是从旧长期笔记蒸馏出的当前有效约定；失效规则见文末附录。
> 相关：`docs/architecture/ARCHITECTURE.md`（唯一一版现状架构与取舍）、`docs/architecture/README.md`（时间线真源）、`docs/architecture/roadmap.md`（现状排期）、`docs/adr/`（决策档案）、`../../GLOSSARY.md`（术语）、`domain.md`（谁在读这些）。

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
- **先文档、后代码**：#31~#33 先收口架构文档、词表、路线图与工程约定，#34 起才动生成任务与数据库投影；票序与依赖见 `docs/architecture/roadmap.md` §1，顺序写进文档、不靠口头约定。
- **文件边界以接缝为依据**：取消「单文件 > 1200 行就拆」阈值，改以接缝为依据，取代 v8 附录 A §5 的 W1 口径（见 `docs/architecture/ARCHITECTURE.md` §5.4）；按「对外 / 内部」划分，包入口只露正常动作、包内互引私有名合法，七个实现文件归入同一内部包即可，不必合并回单个大文件。
- **数字先复核再写**：对外动作与名字数量、入口模式同步点数量、外部引用条数一律以实测为准；写文档前先 `git grep` 复核，不沿用旧报告里的约数。本轮实测：`job_service.__all__` 为 9 个正常动作（`job_ticket` / `job_wait` 另有公开面）；13 个核心标识符在 `frontend/src/` 下 #44 时点实测 **154 行 / 23 文件**、#49 终检复测 **199 行 / 23 文件**；四入口字面量在 `frontend/src/__tests__` 下 #44 时点实测 **151 行 / 13 文件**、#49 终检复测 **213 行 / 13 文件**（两轮文件数一致）；新增快照字段的同步点实测**至少 8 处**；加第 4 个入口的 `714cef0` 为 `33 files changed, 1237 insertions(+), 192 deletions(-)`。这些数字只作旧清单退役前的对照，新形态按 §6 的一张登记表描述；#31 外部引用分布在 8 个文件、改动 13 个 M 文件（含额外发现的前端注释）；再导出块在 #40 已删空，别再引用「25 行 / 15 行 / 11 个动作」等中间数。
- **路径扫描白名单**：终检按类别排除三类写法，别误判成悬空路径——`backend/secrets/master.key` / `deploy/.env` 是运行时生成物（`.gitignore` 排除）、`README.md` / `SKILL.md` 是文档内简写（相对当前文档 / 技能包目录）、`frontend/*.html` / `frontend/src/**` / `scripts/deploy/{backup,restore,release,smoke}.py` 是通配或明确标注「从未落盘」的历史叙述。依据与守卫见 `backend/tests/test_closeout_docs.py`。
- **用例基线现取现算**：收口时按 `--collect-only -q` 的当前收集数现取，连「`def test_` 文本计数 / 参数化展开差额」整段一起重算，不沿用旧总数；本轮实测数字见 §10。
- **加列不是单向门**：单向门指删列 / 退役列（回滚靠备份库文件、不设兼容期）；新增列属于加列，评审不拿迁移成本当架构理由。
- **一次改动要收口**：代码、架构文档、路线图三者同步，谁都不滞后；与 `docs/architecture/ARCHITECTURE.md` §1 目标 / §4 契约及 `docs/architecture/roadmap.md` §0.6 同口径。

## 2. 后端分层与数据层

- `api/` 只解析 / 校验 / 封装 → `services/` 业务 → `repositories/`（唯一碰模型与查询的地方）。
- Pydantic 模型集中 `api/schemas/`（按领域拆成 9 个子模块，`__init__.py` 是唯一出口），被引用的模型必须先定义；Router 各自带完整 prefix，`main.py` 不叠加。
- 仓储返回模型对象或 frozen dataclass 投影，不返回裸 dict。
- 仓库是绑定 `Session` 的实例方法：不许绕过 Engine 的直连（`sqlite3` / 原生 DBAPI / `pymysql`）、不许 `.commit()` / `.rollback()`、不许 SQL 字符串字面量（守卫 C1/C2/C3）。
- 事务边界唯一落点是 `session_scope()`（正常提交 / 异常回滚 / 必定关闭）：仓库只管翻译，服务层负责「一个业务操作 = 一个事务」；不许在 `async def` 里跨 `await` 持有（C4）。
- 引擎参数固定 `expire_on_commit=False`（防 `DetachedInstanceError`）+ `autoflush=False`（要自增 id 就自己 `flush()`）。
- `max_overflow=0` 是承重设计：运行期一条连接都不许新建（池满就报错，连接数问题不推迟到服务端 `max_connections`）；启动路径必须在 `prepare_schema` 之后调 `warmup(engine)`。
- 数据层是 SQLModel：时间工具在 `core/clock.py`，引擎 / 事务在 `core/engine.py`，连哪个库在 `core/database.py`。
- 列类型只走 `core/types.py`：`short_text(n)`（可索引）/ `long_text()`（大段文本）/ `timestamp_text()`（=32）；`models.py` import 时 `assert_metadata_is_portable` 自检，违反即抛错（v8）。
- 大段文本上限 60KB（字节），闸门在 `core/limits.py::ensure_text_within_limit`，接在**统一正文写入入口**（`document_store.write_document`，三种 mode 都过闸）与 `persist_progress/finish/fail`，超限抛 `INVALID_REQUEST`（v8；#81 之前就地改写能绕过这道闸）。
- upsert 只走 `core/dialect.py`：`upsert_values` / `upsert_select`，仓库里不许再 import `sqlalchemy.dialects.*`；必须显式给 `conflict_cols`（不带目标的 `DO NOTHING` 语义更宽），只准用在「该表只有一个唯一约束」的表上（契约 C-D1）。
- **唯一部署后端是 Postgres**（v9 起，票 #75~#78）：SQLite / MySQL 的方言分支、迁移链、测试基座与备份链都已删除；`SUPPORTED_BACKENDS` 只含 `postgresql`，非 Postgres 地址在 `core/database.py` 就被拒绝并给出迁移指引，旧 SQLite 库拒绝启动。四模块边界仍在 = `core/database.py`（连哪个库）/ `dialect.py`（语句）/ `types.py`（列声明）/ `schema.py`（迁移），意义是「方言知识只出现一处」；可移植性台账与 C-D1~C-D6 见 `docs/architecture/ARCHITECTURE.md 附录 A`（v8 原文，结论已被 #78 取代）。两条列类型纪律继续守：索引列必须 `short_text(n)`（n≤768）、大文本（`long_text()`）不得带 `server_default`。
- `get_settings()` 走 lru_cache：改环境变量后必须 `cache_clear()`；`prompts.py` 走 `str.format()`，字面量花括号写 `{{ }}`，给 LLM 的指令用 `<!-- -->` 注释。

## 3. 迁移（Alembic）

- schema 只走 Alembic：新表 / 新列写 `backend/migrations/versions/`，入口 `core/schema.py::prepare_schema`，迁移在 `lifespan` 里跑（重启 = 跑迁移）。
- 旧的 SQLite 库（含旧 `DB_PATH` / 旧 `prdwright.db` 文件）在配置阶段就拒绝启动；Postgres 里结构不符时由 `SchemaMismatchError` 拒绝启动并打印逐条差异。不要再写兼容垫片。
- 迁移链只有**一份 Postgres 基线**（`0001_baseline.py`，由 autogenerate 从模型生成并人工修环形外键）；新表 / 新列写**新的**迁移文件，不许改基线。
- 后续迁移仍要人工复核：autogenerate 对外键 `ON DELETE`、主键 `nullable` 与表达式索引有已知反射缺陷。不再有 batch 模式（`render_as_batch`）与 `sqlite_autoincrement` 的坑——那两样随 SQLite 后端一起删除，C10 守卫禁止它们回来。
- 删列只用原生 `ALTER TABLE … DROP COLUMN`：`PRAGMA foreign_keys` 在事务内是空操作，「关外键 + 重建表 + 改回」那套在步骤内关不掉。
- 删列 / 退役列是单向门：删完旧代码读不到该列，回滚只能靠恢复备份（`pg_dump` 归档），不设兼容期；**加列不是单向门**，新增列不拿迁移成本当架构理由。
- 迁移用例别写死「第几步」：真假源是 `alembic_version`，新迁移比对 head 而不是抄字面量。
- 守卫 C9 = 旧迁移链不许回来（文件存在性 + AST，docstring 豁免）；守卫 C10 = 单后端不许回潮（`versions/` 只许一条 PG 基线、迁移与生产代码里不许 SQLite / MySQL 专属写法与导入）。

## 4. LLM 与 SSE

- 只经 `llm_factory.get_llm()`；默认 ark + `doubao-seed-2-1-turbo-260628`（该模型不收 `thinking.type:"auto"`，只认 `disabled`）。
- 流式唯一入口 `stream_reply.py`；出错只发 `error` 不发 `done`；`on_finish` 仅成功时调、且在 `done` 之前。
- 区分 `stage_chunk` 与 `chunk`：前者是当前阶段的原始输出（审核意见 / 改写稿），只进阶段输出面板；混用会把审核 JSON 写进正文。
- 下游文档开流首条必须是 `stage`：先 `stage(outlining)`（三份产物都有大纲，票 #91），随后 `stage(writing)`；阶段输出归 `JobRuntime`、换阶段即清空、不落库；重连恢复正文必须 `finalized_text + live_buffer`。大纲文本走既有的 `stage_chunk(outlining)`，不进正文 `chunk`。
- 流式工具调用必须逐片相加（`AIMessageChunk.__add__`）：整批塞进一个 chunk 会拆成「空调用 + 废调用」，症状是「模型调了工具、选项卡一张不弹」；假模型必须复现真实分片形态。
- token 计量唯一落点 `astream_text`；方舟 `include_usage` 的用量帧 `choices` 为空 → 先读 `usage`，别 `continue`。
- 预算检查在 `astream_text` 开头、记账在末尾；超顶复用 `INVALID_REQUEST`（别标 `LLM_UPSTREAM_ERROR`，否则前端白重试）；`max_retries` 设 0；`text_emitted` 守卫：yield 过增量就绝不重来。
- 需整段结果（JSON 回填等）用 `document_service._collect_reply()`（拼增量 + 截断检查）。
- SSE 分帧唯一实现 `api/sse.py::make_sse_generator`（已带 `X-Accel-Buffering: no`）；事件 `status/chunk/part/stage/review/done/error` + 末尾 `data: [DONE]`；服务层与路由层两层异常兜底；前端用原生 `fetch` + `ReadableStream`（axios 不支持流式读）。
- 任务订阅的事件**带 SSE `id:` 行**（票 #86）：它是 `job_events.id`（全局游标），只作「断线重连从哪接」用，**不进 JSON 体**（`StreamEvent.event_id`，`exclude=True`）。客户端带 `GET /api/jobs/{id}/stream?cursor=N`（或 `Last-Event-ID` 头）续读 → 服务端只发 `id > N` 的事件；不带游标 → 先快照 + 快照游标，不回放历史。数据永远来自 `job_events` 表（按游标读），`JobBus` / 数据库通知**只作唤醒**；通知丢了只是延迟变大，轮询兜底。清理只删 `job_events`（终态后保留 7 天），权威正文不动。
- 心跳 `: ping` 是 v7 必做，只能走 `api/sse.py` 这个出口。
- 三家 LLM 抽象、路由 / service 分离、SSE 两层兜底、技能包驱动 + 资源缺失降级：别动。

## 5. 生成与审核管线

- **PRD 整篇化（票 #92）**：PRD 与接口文档 / 提示词共用同一条整篇生成路径——一次模型调用写完、一次整篇审核、按需整篇改写一次。分章并行、章级 `stage` / `part` 归属、逐章重试与前端多章看板全部退役（`prd_section_writer.py` / `prd_section_parallel.py` 已删除）；章节只作文档内部结构，结构真源回到技能包模板（`prd-template.md`）。
- 两条轨：`chunk` 是过程字（可能缺标题）、`part` 是权威正文；票 #92 起 `chunk` **不带**章节归属，`part` 只剩 `{text, replace}` 整段覆盖语义，三份产物一致。
- `stage(writing)` 每份产物一次；没有按章分缓冲，`_draft` = 定稿正文 + 单条实时缓冲；快照与进度里的 `completed_sections` / `live_by_section` 随分章链路退役（#92 停写，#94 的迁移 `0008` 把 `generation_jobs.completed_sections` 列删掉）。**分章遗留字段一律不许长回来**：`failed_sections` / `completed_sections` / `section_id` 在结论、回执、快照、任务行与前端类型里都已删除，守卫在 `test_review_unify.py::test_chapter_leftover_fields_are_gone` 与 `dead-code.spec.ts`。
- 失败语义：整篇生成的自愈 / 重试口径不变（`stream_reply._astream_chunks`）；失败收尾照样把已产出正文升格为「部分完成」版本（#85），不再有「逐章占位继续」。
- **分批续写（票 #93）**：撞上模型单次输出上限（结束原因 `length` / `max_tokens`）时**接着写**，不把整篇判失败；唯一实现在 `stream_reply.iter_continuation_events`，三份产物共用，续写请求由 `document_service._continuation_messages` 统一构造（原提示词 + 已写正文**尾部** + 「接着写、不要重复」）。判据只能是**模型返回的结束原因**（`stream_guard.extract_finish_reason`），**不许用字数阈值猜**——阈值会在偏短文档上误判。续写批次仍报 `stage(writing)` 并带 `data.continuation`：进度不回起点、正文不清空、前端不许把它渲染成新章节；接缝重复由 `_SeamGuard` 逐字去重（只丢「紧挨在正文末尾、逐字存在」那段，其余一个字节都不动），提示词只管「换个说法复述」。**批次级重试 ≠ 任务级退避**（#88）：前者在同一批里从断点再要一次（`MAX_BATCH_ATTEMPTS ≤ 3`，不落库、不等待），后者把整条任务退回排队等 `next_run_at`；重试用尽 → `error` 且不撤已写正文（#85 照旧落「部分完成」）。审核 / 改写 / 修复**不续写**（要整段结果）。
- `review_enabled=False` 时连 `review` 事件都不发（报「0 个待处理问题」会被读成「审过了、没问题」）；因此那一版也**不写** `review_failed_at`——「没审」不是「审失败」，判据是事件上的 `attempted` 标记而不是结论空串。
- `review_enabled` 对三份产物都生效且口径一致：`False` 就是「不审不改」，`None` 由后端配置决定（只有 `False` 才是关）。
- 只有 `high` 触发改写（`REVIEW_REWRITE_MIN_SEVERITY`，三份产物共用一处判据 `review.triggers_rewrite`）；`verdict` 如实报、只是不动笔；改写后不重审。**三份产物的审核 / 改写 / 修复是同一条链路**（`services/document_review.py` + `document_service.fix_document_stream`）：产物差异只由 `ProductContext` 表达，不许按产物分支出第二套实现——「只有 PRD 那样」正是分章时代留下的病。结论形状对三份逐字相同：`kind / verdict / summary / issues / rewritten / reviewed_at / attempted`。
- 审核写批注、不复述正文：problem 条目带 `where`（标题路径，取值域由提示词枚举的章节清单限定）、禁止摘录原文、`detail` 限长（解析时截断）；severity 是改写门槛唯一输入，不设 `low`。
- 审核的 max_tokens 只能靠换实例生效（`astream(..., max_tokens=)` 不生效）：落点是 `DocumentService.review_llm` 惰性 property，在后端配置页、不在 `.env`。
- 整篇链路分节改写：只重写 `where` 命中的节、按标题拼回；`part{replace}` 必须发拼好的完整正文（前端无条件整段覆盖）；两条兜底退回整篇重写——`where` 一条都定位不到、拼回后短于 `_MIN_KEPT_RATIO`；`DOC_SECTION_REWRITE_ENABLED` 一键回退。
- 只改 N 节时，未被改写章节的待确认项必须合并保留（`pending_items.finalize_items`）。
- 骨架化 `doc_skeleton.py`（纯字符串、零 LLM 调用）只用在审核侧：标题整行 / 表格整行（只留首列会丢接口路径）/ 条目整行 / 含数字行 / 代码块只留围栏首行；撰写侧的需求输入不动（唯一事实来源），撰写侧要带 `format_field_contract`（枚举递归扫）；`skeletonize` 喂 fenced JSON 会静默吐空。
- 大纲 `doc_outline.py`（票 #91，纯字符串、零 LLM 调用）：三份产物的阶段顺序统一为「大纲 → 撰写 → 审核 → 改写」；大纲只**抽取**上游已有结构，锚点是上游原文的整行（显示才截断），`verify_outline` 后校验查不到出处即降级「待确认」；骨架不许另立一套（接口文档五节、提示词四节必须能在对应生成提示词 / 技能包里找到，用例守着）；抽不出结构时返回 `None` → 只发一条说明性的 `stage(outlining)` 后直接 `stage(writing)`，**不发 `error`**。不要改成「让模型写大纲」——那会把「不编造」重新压在一句提示词上。
- PRD 的整篇改写走 `fix_document_stream`（整篇重写一次，注入当前结论、上游全文与当前待确认清单）；按章改写的提示词与占位符机制已随票 #92 退役。
- 待确认清单唯一实现 `services/pending_items.py`：`extract()` 剥离全部块并按 `{prefix}-{n}` 重分配 id、坏 JSON 与未闭合块跳过；`id` 由后端分配、不信任模型的 id；抑制器流结束仍在块内则什么都不输出。
- 清单三路可达前端：`done.data.pending_items` → `snapshot`（`generation_jobs.pending_items` 列，`_snapshot_event` 与 `_db_snapshot_event` 两条路都要带）→ `GET .../documents`。
- 澄清档案 `doc-clarification` 的 `opening_fields` 必须与模板占位符一一对应（多一个 KeyError、少一个静默填空）；`continue-stream` 也要带 `profile_key`，且 `profile_key` 校验必须在构造 SSE 之前。
- 澄清改写走 `clarify` 任务（复用 job 框架，不是裸 SSE）→ `apply_clarification_stream`；「只动涉及章节」只能靠提示词约束（代码保证不了），提示词里写「逐字保留」。
- 技能包：归属与资源由 `skill.yaml` 自述（`applies_to` / `references[]`），加包 = 加目录、别加中心注册表；唯一入口 `load_skill_artifacts(kind)`，缺件返回 `None` 降级且降级要在回执上看得见；规范整段注入、不检索，改写 / 澄清按白名单 `format_rewrite_reference`。
- `prompts` 生成线的占位符（`prd_content` / `api_docs_content` / `pending_spec`）是跨文件契约：技能包版与 `prompts.py` 兜底版少一个不是「那节空着」而是 `KeyError`；留空填 `NO_API_DOCS_NOTE`。
- `optimize` / `clarify` / `auto-clarify` 的 `kind` 只说「做了什么」，目标产物取 `doc_kind`（回执与落库共用 `_target_product_kind()`）；资源存在性不进缓存。`review_fix`（修复：追加版本）与 `review`（重审：只更新该版本结论）同口径，前者 `doc_kind` 还决定「注入哪份上游全文」，后者不改正文。
- 生成 / 修复的输入只从服务端取：库内产物取当前版本正文，导入入口的材料取方案上保存的录入文本，PRD 的需求输入同理由服务端取。请求体不再携带上游正文与需求正文；缺方案或材料未落库 → 明确 400，不空跑。**解析发生在建任务那一刻**（`POST /api/jobs` 里、造单之前，结果随任务单走、**不进** `request_params`）：开流时才查库会把「缺材料」变成一条流内错误，而那时状态码已经是 200。按澄清结果改写（`clarify`）的对话同样归服务端——按 `doc_kind` 定位 `prd-clarify` / `api-docs-clarify` / `prompts-clarify` 那条线程从 `chat_turns` 取，前端不再回传 `history`；**三份产物的生成只有任务通道**（`POST /api/jobs` + `GET /{id}/stream`）：四条旧的非任务通道（`generate-api-docs-stream` / `generate-prompts-stream` / `generate-prd-stream` / `generate-prd-from-summary-stream`）连同请求模型与前端封装**已删除**（#61，按 §5.5 三条判据），非任务 SSE 只剩对话与 `optimize-document-stream`。四个服务方法仍在、仍被对应任务复用（`_build_event_stream` 直接调，`generate_prd_stream` 就是整篇生成）——**删路由不删能力**，反向守卫在 `backend/tests/test_cleanup_dead_code.py`（OpenAPI 里不许回来 + 服务方法必须还在）。**就地改写的三个动作例外**：`optimize` / `clarify` / `auto_clarify` 的「被改正文」仍随请求体带来（`document` 字段）——它吃的是**目标**，可能含用户还没落库的手改；归服务端的只有上游与 `clarify` 的对话线程，别把这层收口顺手扩到 `document`。
- 交付：`GET /api/solutions/{id}/export` → ZIP = 三份 md + `README.md`；只导用户看得见的；目录名必须消毒（`_safe_name` 抹 `..`、拦 `con` / `nul`）；上限 50MB、CSV 带 BOM。
- 上游正文变化（含手改，按 `content_sha`）只让下游 `stale=true` 亮黄条：不删除、仍可交付、只汇总提示不硬拦。
- 接口 / 提示词：整篇生成 → 审一次 → 有问题改写一次；**审核结论随版本保存、一版一条**（三份产物形状逐字相同，一版一条），SSE `review` 只负责订阅期间的即时展示，刷新后从文档接口读；**审核失败不写结论**（没有结论 ≠ 通过，界面标「审核失败」）。面板在「待确认问题」之下、正文之上（`ReviewConclusionPanel`），**四态恒渲染**：通过 / 有问题 / 审核失败 / 未审核；两个动作都在折叠头常驻、不可点时**就地置灰**——「修复」只在「有问题」可点，「重新审核」只在失败 / 未审核 / 通过可点（别把按钮藏起来，也别猜默认值）。**按结论把关时要说明原因**（`title` 与面板里的说明写清为什么不能点）；**按「别处有任务在跑」把关时不写文案**：灰的原因不在这一份的结论上，见 `ARCHITECTURE.md` §5.11。面板同时显示**审核时间**（`reviewed_at`）与**当前版本号**：结论要能回答「哪一版、什么时候审的」。失败态只显示「这次为什么没有结论」，**不渲染 `props.review` 的 summary / issues**（那是上一次的结论，摆在「审核失败」下面会被读成本次结果）。
- 结论面板的数据源是**文档记录**（`DocumentRecord.review` + `reviewFailedAt`），`review` 事件只做即时更新（`documents.setReview` / `setReviewFailed`）；解析器唯一实现是 `frontend/src/utils/review.ts::parseReview`（`services/api.ts` 只再导出，`documentService` 直接从 utils 引——反向 import `api` 会成环）。「审核失败」是**落库事实**：失败时间写在被审那一版的 `document_versions.review_failed_at` 上（US3，迁移 0007），拿到新结论时清空，因此刷新 / 换设备后仍显示「审核失败（时间）」；读侧三格（结论 / 失败时间 / 版本号）在 `stores/documents.ts` 收成**一张记录表** `reviewSnapshots`，别再拆回三张平行 `Record`。**两种「审失败」写同一列**：`review` 任务的失败与**生成链路的审核降级**（PRD / 接口文档 / 提示词）都落 `review_failed_at`——用户看来都是「审了、没成」。「没审」与「审失败」由**显式标记**区分：`review` 事件带 `attempted`，`JobRuntime.review_attempted` 记下来，收尾按 `review is None and review_attempted` 判；显式关闭审核（`review_enabled=false`）连事件都不发，因此不写、仍是「未审核」。别用判定空串去猜（空正文那条路也是空串，但它不是失败）。三份产物的审核失败同口径（票 #92 后 PRD 不再有章级降级）：不写结论、写 `review_failed_at`。
- 重新审核（`kind=review`）= 只审不改的独立任务：输入由 `job_inputs._review_inputs` 取（`document` 是被审正文 + 与生成同一套上游 + **当前版本的待确认清单**，见 `_current_pending_items`——重审要判「清单漏登记」），三份产物都走 `document_review.stream_review_only` 整篇审一次（票 #92 前 PRD 是逐章审再汇总，已退役）；收尾走 `document_store.commit_review`（只写当前版本的 `review` 列）。**审核失败判任务失败、绝不按通过处理**——生成链路那条「审核失败按通过」的口径不适用于「唯一的产出就是判定」的重审。**结论只写到被审的那一份正文上**：被审正文在建任务那一刻快照，收尾时用**条件写**（`expected_content_sha`）比对当前版本指纹；不一致 → 任务失败（`error_code=CONTENT_CHANGED`）、文案「正文已变更，本次审核结论未写入，请重新审核」、**已有结论原样保留**。事前比较（`job_finalize._review_body_changed`）与写入 SQL 各一道，**两道都判失败**（不是只记日志：写不进去却报 succeeded，用户看到的是一条「成功但结论没变」的记录）；目标缺失（`missing`）同理。失败本身写进 `review_failed_at`（US3）。**需求澄清线程与生成侧同一份内容口径进重审提示词**（PRD 的审核提示词里带一节「需求澄清对话」）：少了它，用户拍板过的细节会被判成编造。
- 四种「没审成 / 没修成」的边界分开写，别混成一个失败：**重审**没跑成 → 任务失败、结论不写（唯一产出就是判定）；**修复的重写**没跑成 → 任务失败、正文与版本都不动；**修复的重写成功而重审没跑成** → 正文照落库、结论留空（任务仍成功，失败时间落进新版本的 `review_failed_at`，面板标「审核失败（时间）」）；**生成时的审核没跑成** → 正文照落库、结论留空（任务仍成功，失败时间同样写进该版本的 `review_failed_at`，刷新后仍是「审核失败」）。四种都不写占位结论。**重审缺上游不 400**：PRD 按「仅依据正文与写作规范核对」，接口 / 提示词在提示词里写「未提供上游材料」——「历史方案也能补审」是用户故事，不该被生成侧那条硬校验挡住。
- **审核只报缺陷，不报未决**（ADR 0003，C-R5）：结论里的每条都是**缺陷**，六类——编造 / 矛盾 / 结构不符 / 空泛 / 推断未标注 / 清单漏登记；**「缺什么、该怎么定」不进结论**，那是未决、归待确认清单。**类别决定严重度**（编造与矛盾 = high，其余 medium），提示词里逐类写死，不让模型升降。**两个时点两套口径**：生成过程中只判前五类；**只有重审与修复后的重审**才判「清单漏登记」，并把当前版本的待确认清单整份注入提示词（清单要到收尾才抽得出来）。「推断未标注」**只判形式**（有没有「（建议）」与一句依据），不评判依据强弱——这是同一件事被反复报的来源。**修复提示词必须写死「不得为缺失编造」**：缺就写「待确认」并登记清单，否则修复会把「未决」填成「编造」。界面上这块标题是「**文档缺陷**」，按类分组、类内按严重度排；**旧结论没有类别 → 归「未分类」，不追溯**（重审一次才按新口径重算）。唯一实现点 `services/review_defects.py`：类别→严重度映射、两时点开关（解析的 `allow_missing_pending` 与服务端丢弃第六类）、清单注入的文本、旧形状归一都在这里；三份产物共用同一条整篇审核链路（`document_review`），提示词只有一份、用 `{pending_section}` 占位符区分两态。
- 澄清提问上限 1-2 个，两处落点必须同步改：后端 `conversation_service.REQUIREMENTS_WRAPPER`（首轮开场白，是最后一条 user 消息）+ 前端 `useClarificationChat.ts::CLARIFICATION_SYSTEM_PROMPT`（开场白更靠后更具体、会盖过系统提示词）；口径由 `test_conversation_prompt.py` 锁死。
- MVP 留空时按 `ui_pages` 推断候选功能清单，逐条在功能点名称后加「（建议）」，优先级 / 验收写「待确认」，`ui_pages` 也为空才退回「待确认」；改这条要同时改 4 处（`writing-rules.md §4`、`prd-template.md 2.1/6.1`、`SKILL.md` 阶段一/四、`field-schema.json` 的 `mvp_features.description`）。
- 未填的题由 `format_form_data` 在已答项后汇总一行「以下题目用户未填写：…」，空串与「键不存在」同等看待；配置读不出来记 warning（降级要看得见）。

## 6. 前端与状态机

- `main.ts` 只挂 `AppV2`；router 只有 `/v2`、`/v2/:id`，阶段流转走状态机、不进 URL；外壳在左侧 `SolutionSidebar.vue`（无顶栏）。
- `types/index.ts` 是契约准绳（不一致时改后端迁就前端）；状态在 Pinia `stores/{workflow,solutions,auth}.ts`；Markdown 统一走 `utils/markdown.ts::renderMarkdown()`（`html:false` 防 XSS）。
- 入口 = 一屏两组四叶子（`EntryStage.vue`）：`从零创建 PRD`（`structured` / `prd-quick`）｜`导入已有素材`（`prompt-debug` / `prd-and-api-paste`）；`prompt-debug` 键名故意不改，新键刻意不叫 `prompts`（那是 `DocKind` 取值）。
- 流程：入口 → 表单 → 对话 → 闸门①摘要 Diff（回填失败返回原摘要深拷贝）→ PRD → 接口文档 → 提示词 → 交付；文档澄清就在对话页。
- **加一个入口 = 给登记表加一行**：入口模式的资料以**一张登记表**为唯一真源，一行一个入口、七类资料为列（① 键、组别与卡片文案，② 落页，③ 进度条步骤，④ 三个布尔：录入页是否在进度条上 / 是否需需求澄清 / 是否需审核改写，⑤ 完成所需文档，⑥ 文案：列表里的模式名 / 返回与改需求两个按钮的文案，⑦ 录入字段）；前端纯函数层是唯一答问者，工作流状态存储、方案存储、文档流水线 composable、应用外壳与入口选择页都向它问，不再各自按入口写死。加第 5 个入口只填一行；缺行、缺列、未知键在问询当场失败，不猜默认值；「未选择」是唯一合法口径，照常给完整流程的展示答案。原「加一个入口要同步约 20 处」的人工清单整段退役，连同「170 个文件:行号落点」一并作废：13 个核心标识符在 `frontend/src/` 下 #44 时点实测 **154 行 / 23 文件**、#49 终检复测 **199 行 / 23 文件**，四入口字面量在 `frontend/src/__tests__` 下 #44 时点实测 **151 行 / 13 文件**、#49 终检复测 **213 行 / 13 文件**。行为断言只跨应用外壳集成接缝（真 router + 点卡 + 屏幕结果）与登记表完整性接缝（半填的假第 5 入口当场被拒），不扫源码、不数落点、不查标识符词频。范围与取舍见 `docs/architecture/ARCHITECTURE.md` §5.8，票序见 `docs/architecture/roadmap.md` §1。
- 加 `ViewState` 同步四处：`ViewState` / `VIEW_STATES` / `STEP_OF_STATE` / `STATE_DOWNGRADE`，漏一处只表现为进度条算错或刷新后进不去。
- 快捷流程 = 同一管线、六步齐全：登记表的 `requiredDocs` 列与 `structured` 一致（三份文档齐备才算完成）、`STEP_OF_STATE['prd-quick']='form'`；`hasRequirementChat()` 只认 `structured`；「AI 对话」那格不能省（找不到就归零 → 停在对话页却高亮「描述产品」）；它不问问题，缺的信息靠「待确认」+ 审阅页「AI 自行澄清」。`frontend/src/stores/workflow.ts` 里「快捷入口只要 PRD」的旧注释与三份齐备的判据互相冲突，该注释已随代码票删除（#47）。
- 进度条「AI 对话」摘到最左、用竖线隔开（纯呈现，`stepsForMode` / `stepIndexOf` / `reachedStepIndex` 未动）：高亮与可点一律按「原始下标」；分隔线用 `border-r`、别插 div；`li` 数量仍为 5。
- 交付页不再由「完成」按钮把关：`requiredDocsReady` 为真时进度条「交付」格可点，审阅页没有完成按钮；**只有三份齐备的入口**弹那条「三份文档已齐备」提示（每个方案每次会话只弹一次，且不自动跳转——正在改稿时跳页比多点一次更糟）；两个导入入口只要一份提示词，齐备时**不弹**这条提示（对只要一份的人说「三份」是假的），入口照旧是进度条那一格。
- `DocKind` 含 `optimize` / `clarify` / `auto-clarify` / `review` / `review-fix` / `chat`（动作），`DocumentKind` 只三份（东西），`isDocKind` 必须认 `chat`；前端 `api-docs`（连字符）/ 后端 `api_docs`（下划线），唯一转换点在 `documentService.ts` 与 `api.ts`。
- 四条澄清线程（需求 / PRD / 接口 / 提示词）全在同一对话页：单 `messages` + 每条打 `stage`，只渲染当前标签那一条；`chatTab`（意图）与 `activeChatTab`（实际显示）必须分开、后者绝不回写；回填只取 `requirement`；`ProgressSteps.canClick` 必须含「当前步骤」；线程作废只在整篇重生成。
- 澄清改写 / AI 自行澄清切到 `generating-<kind>`，完成回审阅；当前实现先以 `isRunInFlight()` 提前拦重入，再 `goTo('generating-prd')` 后 `await generatePrd()`（见 `frontend/src/composables/useDocumentPipeline.ts`）。写这个顺序是因为先切到生成视图、再让这次发起成为唯一发起，而**不是**因为旧 `docGenerating` 忙碌闸门会拦跳转：那个闸门已随 `frontend/src/stores/runs.ts` 退役，`goTo()` 不因忙拒绝跳转（见 §6 运行态条目）。旧文里的 `clarifyTarget` 字段也已删，源码只剩退役说明；改写失败也留在生成视图。
- 「修复」与「重新审核」同为任务动作：都先 `isRunInFlight()` 拦重入、切到 `generating-<kind>` 看流式进度，收尾回审阅；修复只追加版本，重审不碰正文。黄条「一键更新」先弹确认（写明旧版本保留在版本历史），再走同一份整篇生成入口。
- 澄清上限与分级 = 后端共享常量 `CLARIFICATION_QUESTION_RULE`，各档案整段引用、别各写数字；等级没有兜底（`IMPORTANCE_ORDER` 是 P0..P6，缺失或非法一律丢弃）；未注册档案名 → 400。
- 澄清选项卡挂 `ChatMessage.asks`（不加顶层快照字段）；无选项时不写 `asks` 字段（恒带 `[]` 会撞破形状断言）；选项说明不许空；卡片里不放输入框；多选答案按选项序拼、不按点选序；写回会话历史的必须是剥离后的干净正文；可交互判据 =「当前线程最后一条消息」。
- `stores/runs.ts` 是「有没有任务在跑」的唯一真源，三层判据 `jobId`·`sessionEpoch`·`docKind`，不许猜默认值；`goTo()` 不因「忙」拒绝跳转、入口切换仍拦；`runs.begin` 不清预览（只 `resetDocTransient()` 清）；`applyJobSnapshot` 三份产物同一条整篇路径（正文 = 定稿 + 实时缓冲，阶段 / 阶段输出从快照回填，只在空着时写），`onChunk` 参数是对象 `{text}`。
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
- 写入语义：整篇生成 = 追加新版本（不再删下游）；`optimize` / `clarify` / 手改 = 就地改当前版本；**修复** = 追加新版本（正文与结论同批写入）；checkpoint / restore = 追加新版本，恢复绝不把指针拨回去（历史只增不改）。
- 审核结论住在版本上（一版一条，`review` 列），重审覆盖同一条、不追加版本；读取面随文档列表与版本详情返回，SSE 事件不是真源。**审核失败时间**同样住在版本上（`review_failed_at`，迁移 0007），生成时的审核失败与重审失败共用这一列。
- 版本来源取值（`document_versions.source`）必须**全部**在 `frontend/src/utils/versions.ts` 有中文说法：它直接取 `runtime.kind`（`prd` / `api_docs` / `prompts` / `optimize` / `clarify` / `auto_clarify` / `review_fix`）或写入口（`manual` / `checkpoint` / `restore` / `migrated`），漏一个版本历史里就出现裸标识符。`review` 不追加版本，因此不会出现在版本行上。
- 「是否追加版本」与「是否删下游」恒等（都由「是不是整篇重生成」决定），`job_service` 收成一个 `append_version` 标志。
- 环形外键：先空指针插 `documents` 行 → 插版本 → 回填指针（`_ensure_document()`）。
- 写正文**只有一个入口**（#81）：`document_store.write_document(mode=append / replace / manual, expected_version_no=…)`——手改、整篇生成、修复、恢复、就地改写都走它；`save_document` / `commit_generated` / `checkpoint` / `restore` 是薄封装。新增产出正文的路径不要自己写库，接进 `_build_event_stream` 的 `done`（收尾经 `_finalize` → `write_document`）。**所有会覆盖当前正文的写入带版本锁**：对不上返回 `version_conflict`（不是「0 行 / 成功」），HTTP 保存回 409 + `CONTENT_CHANGED`，就地改写让任务判失败——旧内容不许覆盖已更新的正文。正文 + 清单 + 回执（就地改写时）同批写入、失败不覆写。
- **收尾是一个事务**（#81）：`_finalize` 用一个 `session_scope()` 收进「状态机条件写 + 账本（保存点）+ 正文版本 + 审核结论」，提交后才结算内存与广播；写正文失败整批回滚、任务保持 `running`（留 `job.closeout_rolled_back`，由恢复接手），**不许把异常吞成 warning 后照报 succeeded**。重审结论写不进去仍判失败（第二笔事务只落失败 + 账本）；就地改写撞版本锁以 `CONTENT_CHANGED` 判失败。
- `save_document`（手改，就地改当前版本、只动正文）与 `commit_generated`（整批写入：`append_version=True` 追加版本 / `False` 就地替换正文 + 清单 + 回执）都只是 `write_document` 的门面；`optimize` / `clarify` / `auto_clarify` / `review_fix` 落地靠请求体 `doc_kind`，取不到 → 不落库也不猜默认值；`review` 只更新当前版本的结论列（`commit_review`，可入收尾事务）。
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
- `SessionDataV2` 不再有 `*Content` / `*RunSummary`；新增字段要按实测同步**至少 8 处**：类型契约（`frontend/src/types/index.ts`）、序列化（`frontend/src/stores/solutions.ts` / `frontend/src/services/solutionService.ts` / `frontend/src/utils/workflow.ts`）、store 的 snapshot / applySnapshot / reset / 相位与内容 watch / 有内容判据 / return（`frontend/src/stores/workflow.ts`）、测试夹具（`frontend/src/__tests__/support/solutionState.ts`）。旧「snapshot / applySnapshot / reset / watch 四处」口径作废。登记表落地后，随入口模式变化的录入字段由这一行的「录入字段」列统一描述。
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
- 「卡死」= `running`（执行中）且（`lease_expire_at` 已过期 **或** `updated_at` 30 分钟未更新）：判据唯一一处是 `generation_job_repo.stuck_condition`；`is_stuck` 只由服务端算（含「仍 running 但租约已过期」这个 #83 恢复器收口之前的窗口），概览页卡死数与任务列表 `?stuck=true` 共用它；自动刷新 10s 轮询不用 SSE。
- 概览页「四个数字」（票 #89）= 排队数 / 成功率 / 平均耗时 / 卡死数，全部由 `GET /api/admin/stats` 从库里算好（前端不推算）：成功率分母只含已定稿 + 真失败，**排除被取代与已取消**；平均耗时只算已收尾（成功 + 真失败），取消 / 被取代不进；卡死数见上一条。卡死数可点，落 `/jobs?stuck=1`（`stuck` 是服务端筛选），不新建看板。
- 级联：`users` → 会话 / 方案（→ 文档 → 版本 / 线程）/ 任务；有意不级联 `admin_audit_log` / `app_logs` / 全局配置；删号必须 `clear_login_failures()`。
- 删除前 `cancel_for_user()` 停在飞任务（只改库不改内存 → 协程照旧烧 token）。七态口径（票 #84）：取消是**两段**——请求把执行中的行落 `cancelling`（不清租约），排队中的行一步落 `cancelled`；执行者在下一个动作点确认才落 `cancelled`（清租约、补一次载荷），租约到期的 `cancelling` 由恢复器兜底。删方案走 `cancel_running` + `_finalize` 兜底（`generation_jobs.solution_id` 无外键）。`error_code` 不再承载「取消 / 被取代」语义，两者各有状态值；二次确认要求手打用户名。

## 10. 测试

- **用例基线现取现算**：报基线前先 `cd backend && uv run pytest --collect-only -q` 取当前收集数，不沿用任何旧总数。**#63 收尾实测（分支 `ticket/63-review-defects-impl`，基于 `integration/spec-63` 的 `6c6e4bc`）**：收集数 **1243**；`git grep -c 'def test_' -- backend/tests` 逐文件计数求和得文本计数 **1065**；参数化展开差额 **178**（1243 − 1065，即一条 `def test_` 被参数化展开成多条收集项后的净增）。三个数都是现取的：本轮新增 `tests/test_review_defects.py` 八条（重审不报已登记项 / 报漏登记 / 生成不判第六类 / 编造抬 high / 中档不改写 / 旧结论无类别 / 修复不得编造 / PRD 空清单仍开第六类），收集数与文本计数同步 +8、差额不变（上一轮 #61 的 1235 / 1057 / 178 从此只作对照）；以后收口仍按 `--collect-only -q` 现取。**#71 收尾实测（分支 `ticket/71-closeout`，基于 `integration/spec-66` 的 `e79a616`）**：收集数 **1261**；文本计数 **1081**；差额 **180** —— 本轮后端只新增一条文档守卫（`test_closeout_docs.py::test_run_view_conclusions_are_in_current_docs`），收集数与文本计数同步 +1、差额不变；前端新增十四条用例（#68 四条：切视图回运行视图 / 压制位与出口 / 收尾不抢页 / 收尾时人就在这份文档上不指错路；#69 两条：三份入口禁用 / 收尾后恢复；#70 五条：指针在时归位 / 指针丢了仍归位 / 停在别的产物也归位 / 多支并行时优先接修复 / 兜底只查一次 / 重审切走再切回也回运行视图），`frontend` 的 `test:unit -- --run` 为 **766 passed / 41 文件**（上一轮为 752 / 41）。默认 `addopts = "-n auto"`，全量在 #77 换 Postgres 基座后约 150 秒（SQLite 时代约 100 秒）；单文件调试加 `-n0`。旧口径里的 1238 / 1059 / 179、1235 / 1057、1234 / 1057 / 177、1228 / 1051、975 / 863 / 867 / 999 / 1006 / 1014 / 1089，以及 955 / 134、1100 / 1110 / 1127 / 1136 / 1140 / 1149 / 1150 / 1175 / 1179 / 1180 等中间数全是过时数字，别再用。**#92 收尾实测（分支 `ticket/92-whole-doc`，基于 `integration/spec-75` 的 `c889ae8`）**：收集数 **1443**；文本计数 **1246**；差额 **197** —— 三个分章测试文件退役（52 条收集项）与散布断言（13 条）净删 65、新增整篇链路 8 条；前端 `test:unit -- --run` **751 passed / 42 文件**（基线 822 / 43），`npm run build`（含 `type-check`）通过。
- 退出码不可信时用 `uv run pytest --junit-xml=...` 判成败（沙箱可能拦 `%TEMP%`；必要时加 `--basetemp=<项目外可写目录>`）。
- 两个人同时跑全量会互相污染（单条从 0.5s 涨到 3s+，`LogStoreHandler.close()` 超时后写进下一用例的库 → `total != 25` 偶发红）。
- 别在 `test_admin_logs.py` 断「全表行数」：每请求都异步写一条 `request.completed`，与 `total==N` 赛跑（单文件绿、全量红）。
- 测试直连库用 `tests/dbsupport.py::connect`（实现已换成 psycopg 的底层连接，行对象同时支持 `row[0]` 与 `row["列名"]`，SQL 占位符用 `%s`）；建库是「**每 worker 一次模板库 + 每用例 `CREATE DATABASE … TEMPLATE`**」（#77 起全量用例跑在 Postgres 上，模板库走真实 `prepare_schema`），别改回每用例跑 alembic / `create_all`，也别改回「拷库文件」。集成库地址来自 `PRDWRIGHT_TEST_POSTGRES_URL`——**维护库地址、不带库名**（缺省 `postgresql+psycopg://postgres:postgres@127.0.0.1:5433`），库名由用例自己建；连不上时**用例直接失败并给出操作指引**（全量用例不允许 `skip`，静默绿比红更难发现）。
- **#77 是「每用例独立库」，不是「每用例独立 schema」**（票面写错了，口径以此为准）：Postgres 的 schema 只是**一个库内部的命名空间**——同一个库、同一套连接池、同一个 `alembic_version` 与 `system_config`，用例之间照样互相看得见，隔离不了任何东西。真正落地的是每用例 `CREATE DATABASE … TEMPLATE`（派生库结构相同、零数据、用完删掉）。按票面去找「每用例 schema」会一无所获。
- **后台调度循环在用例里默认关**（`tests/conftest.py` 置 `JOB_SCHEDULER_ENABLED=false`）：`services/job_scheduler.py` 的三个周期任务会扫全库的排队行 / 过期租约、清理过程日志，一千多条用例各自起三个只会互相抢行、并在用例收尾时还在借连接（`in_use == 0` 的泄漏守卫会响）。要验循环本身，显式把它打开——`tests/test_job_scheduler.py` 的做法是 `monkeypatch.setenv("JOB_SCHEDULER_ENABLED", "true")` + `get_settings.cache_clear()`，再 `with TestClient(app)`，或者直接 new 一个 `JobScheduler` 手动驱动 `run_once()` / `recover_expired()`（不等真实秒数）。生产默认是**开**的。
- `--durations` 输出降序，要看头部（`tail` 看到的是第 30 名）；真实长尾是 `llm_retry_delay` 1.5s × 重试，并行压不掉。
- 前端：`npm run build` = type-check + build（`vue-tsc` 含测试文件）；`test:unit` 底层是 vitest，CI 必须加 `--run`；全流程离线、不需要任何 LLM Key；改前端后必须跑一次后端用例，改契约 / `ViewState` 时先改测试夹具。
- 前端测 AppV2 必须有真 router（`createMemoryHistory`）且 `await router.push(path)` 之后再 mount；弹窗 Teleport 到 body 要查 `document.body`；表单提交用 `dispatchEvent(new Event('submit'))`；编排测试不要用「填充示例」准备数据；`clickButton` 默认 `exact = false`（「包含」匹配），入口卡也没传 `true`，靠固定 DOM 顺序的用例消「我有 PRD」前缀歧义；旧文「入口用 exact」与现状不符，别照它写新用例并已列入文末「已失效」；`mountExistingSolution(snapshot, id, documents)` 的第三个参数会覆盖 `fetchDocuments` 的 mock。
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
- 备份用 `pg_dump -Fc`（`deploy/backup.py`），不能 `cp` 数据目录、也不再是 SQLite 时代的 `VACUUM INTO`（#78 起后端只剩 Postgres）；追加自检 = `pg_restore --list` 校验归档可解析 + 读 `alembic_version` + 记 sha256；主密钥保留每一把。回滚演练入口是 `deploy/drill.py`（`make drill`），预案见 `deploy/ROLLBACK.md`。
- `PRDWRIGHT_MASTER_KEY_PATH` 走 `os.environ` → `.env` 读不到，必须 `EnvironmentFile`；`PRDWRIGHT_SKILLS_DIR` 走 `lookup_env`（两条路都通）。
- 环境硬事实：`requires-python >=3.12`（Ubuntu 22.04 不够，建议 24.04）、Node `^22.18 || >=24.12`、无 requirements.txt（`uv sync --frozen`）。
- 管理端挂 `/admin/` 要两处一致：`vite base: '/admin/'` + `createWebHistory(import.meta.env.BASE_URL)`（Vue Router 的 `normalizeBase()` 不跟随 Vite 的 base）。
- Nginx：SSE 三行（`proxy_buffering off` / `read_timeout 1800s` / `gzip off`）+ 服务端 `: ping` 心跳；`assets/*` 长缓存、`index.html` 必须 `no-store`；`/docs` 生产 404（后端不注册 + 白名单移除 + Nginx 兜底）。
- `deploy/` 目录是 v9 的交付物（compose、反向代理、env 样例、启动脚本、备份脚本、README），**已随 v9 落盘**：`ls deploy` 与 `ls deploy/docker` 都看得见，交付形态的唯一说明在 `docs/architecture/ARCHITECTURE.md` §3.6。v7 文档里那几份（`backend.env.example` / `systemd/prdwright.service` / `nginx/prdwright.conf` / `scripts/deploy/{backup,restore,release,smoke}.py`）**从未落盘**，只作当期的形态记录，别照那些路径找文件。`scripts/deploy/` 目录现只保留 `smoke.sh`，不要把它读成整套交付脚本仍在。

## 已失效（勿再引用）

- `core/db.py` 与那个 444 行连接池实现的 `core/database.py`：已删（v6 换 SQLModel）；旧说法「`core/database.py` 已删」已作废 —— v8 里该文件名以「连哪个库」的四模块之一重新存在。
- `SCHEMA_SQL` / `init_schema` / `PRAGMA user_version` 的 20 步手写迁移链（`core/migrations.py` 的 `MIGRATIONS` 清单）：v6.1 整体删除，被 Alembic 取代（`core/schema.py::prepare_schema`）；「自研迁移比 alembic 省」的论证同时作废。
- `solutions.data` 存全量 `SessionDataV2` JSON（后端不解析 data）：`data` 已退役（create 写空串、update 不碰），正文住 `document_versions`。
- 三份正文存 `documents` 的正文列：v6 删列（单向门），被 `document_versions` 取代。
- 闸门② RAG（`retrieve-api-docs-rag` + `core/api_doc_knowledge.json` 本地词法检索 + `RagResultPanel`）：已整条移除。
- 四条旧的非任务生成通道（`generate-prd-stream` / `generate-prd-from-summary-stream` / `generate-api-docs-stream` / `generate-prompts-stream`）「仍挂载、只收 `solution_id`、删路由属清理票」：**已删除**（#61）。请求模型与前端封装一并退役，服务方法保留供任务复用；「它们还挂着」的旧说法别再引用。
- 整篇生成删下游、`_write_document` 一律传 `invalidate_downstream=True`：已被「下游只置 `stale` 亮黄条、不删、仍可交付」取代。
- `quality_gate` / 需求基线签字 / `manifest.json`：裁决不做（`quality_gate` 恒 `'{}'` 的旧口径一并作废）。
- 三家 LLM 自动 failover：已取消，不再跨供应商换线。
- `PRD_SECTION_*` 系列（分章撰写 / 改写 / 审核提示词、五个 PRD 专用配置键、`prd_section_writer` / `prd_section_parallel` 模块）：已退役（票 #92 整篇化）。
- `clarification_ask.render_ask_spec()` 这个旧名：已退役，现名 `render_ask_tool_instruction()`。
- 测试建库用 `init_schema()` 或每用例 `create_all`：被「`tests/dbsupport.py` 模板库 + 每用例拷贝」取代，后者又在 #77 被「每 worker 模板库 + 每用例 `CREATE DATABASE … TEMPLATE`」取代（见 §10）。
- 用例数基线 93 / 217、477 / 588、863 / 867、975、999、1006、1014 / 986、1089、1100 / 1110 / 1127 / 1136 / 1140 / 1149 / 1150 / 1175、1228 / 1051 / 177、1234 / 1057 / 177、1238 / 1059 / 179、1235 / 1057 / 178（及配套的「文本计数 955 / 134」）：全部作废。本轮终检按 `--collect-only -q` 现取，连文本计数与参数化展开差额一起重算，实测口径见 §10。
- v3 期的排期只是当时的口径，别当现状真源；按层路线图已随九层目录一并删除。现行排期看 `docs/architecture/roadmap.md`；决策与取舍看 `docs/architecture/ARCHITECTURE.md` 的「取舍」节与 `docs/adr/`。
- 原有的按层决策记录文件与按层时间线记录文件**均已删除**（决策记录目录随后也整体移除）；按层文档目录随后也整体移除，独有内容并入 `docs/architecture/ARCHITECTURE.md` 附录、`docs/architecture/README.md` 与 `docs/adr/0001-architecture-docs-consolidation.md`；按旧路径引用一律无效。
- **本轮收口作废的旧口径**（正向条文只写一次，见 §1「先文档、后代码 / 文件边界以接缝为依据 / 数字先复核再写 / 用例基线现取现算 / 加列不是单向门」、§3「加列不是单向门」、§10「基线现取现算」）：「按单文件 > 1200 行阈值拆文件」（取代 v8 附录 A §5 W1，见 `docs/architecture/ARCHITECTURE.md` §5.4）、「报告里的约数可直接抄」（对外动作、再导出名、入口模式同步点、外部引用条数）、「用例基线沿用旧总数」（1089 / 1100 / 1110 / 1127 / 1136 / 1140 / 1149 / 1150 / 1175 及文本计数 955 / 差额 134 整段）、「25 行再导出块 / 11 个对外动作 / 十来处入口同步」（#40 后为 9 个对外动作、再导出块删空；入口模式人工清单整段退役，改为一张登记表、一行一个入口，旧「约 20 处同步点 / 170 个文件:行号落点」按实测更正为 154 行 / 23 文件，四入口字面量在 `frontend/src/__tests__` 下为 151 行 / 13 文件，快照字段同步至少 8 处）、「加列是单向门」（单向门指删列 / 退役列），以及「#31~#33 与代码票可并行」「#44 之后可跳过文档直接改代码」「入口用 exact」「`goTo` 早于发起任务是因为旧忙碌闸门会拦」（旧闸门已拆、`goTo()` 不因忙拒绝跳转）「`clarifyTarget` 仍在清单里」（字段已删）——全部作废；票序与总原则见 `docs/architecture/roadmap.md` §0 / §1（#44 → #45 → #46 → #47 → #48 → #49 严格串行）。
