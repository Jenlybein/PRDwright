"""数据库 schema 迁移。

为什么需要它
------------
`core/db.py::init_schema` 只有 `CREATE TABLE IF NOT EXISTS`——而这句话对**已存在**
的表是空操作：给旧表加一列、补一个约束，都不会落到现网库里。于是「新建的库」与
「跑了一阵的库」会随着时间悄悄分叉，而分叉往往只在运行期以很难定位的方式暴露。

本模块给这个库补上「怎么演进」的地基：用一个单调递增的版本号（SQLite 自带的
`PRAGMA user_version`）记住「已经应用到第几步」，启动时只跑还没跑过的步骤。

约定
----
- `user_version = 0` 表示「只有 `init_schema` 建出的基线」，因此**迁移版本从 1 开始**。
- `MIGRATIONS` 的顺序**即执行顺序**，版本号必须是 `1..N` 连续整数；新增结构一律
  追加到列表末尾，**不要修改历史步骤**（改了会让已升级的库与新建的库再次分叉）。
- 每步执行成功后**立即**写回 `user_version` 并提交，因此中途失败只会停在上一档，
  重跑从断点继续——重启天然幂等。
- **新表不进 `init_schema` 的 `SCHEMA_SQL`**：否则「新库靠 init_schema、老库靠迁移」
  会在两处各建一次同一张表，迟早对不上。新库也从版本 0 起步，逐条跑迁移。

失败处理的分野
--------------
- **结构变更（DDL）失败 → 抛出异常、中止启动**：带着半个 schema 起服务，只会把
  问题拖到运行期，而且更难定位。启动失败是这里最便宜的失败方式。
- **数据回填失败 → 逐行容错**：回填读的是旧的、不受本服务控制的用户数据，
  某一行损坏不该拦死整个服务的启动。具体策略由各步骤自己实现。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from backend.core.chat_kinds import is_valid_role, is_valid_stage, normalize_stage
from backend.core.db import connect, utc_now_text
from backend.core.digest import content_fingerprint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    """一步迁移。

    `apply` 只做一件事：在给定连接上把结构或数据改成目标形态。它**不需要**自己
    写 `user_version`、也不需要提交——那是 `run_migrations` 的职责，收在一处才
    不会漏掉某一步。
    """

    # 目标版本号，必须从 1 开始连续递增
    version: int
    # 简短名字，写日志用（如 `documents_table`）
    name: str
    # 实际动作；抛异常即视为这一步失败
    apply: Callable[[sqlite3.Connection], None]


def _create_documents_table(conn: sqlite3.Connection) -> None:
    """v1：建 `documents` 表。

    此前三份产物正文（PRD / 接口文档 / 提示词）只存在前端的整份会话快照
    （`solutions.data`）里，后端**从不解析**那份 JSON——于是服务端对文档无能为力：
    没法派生、没法对账、没法追历史，连「这份方案有几篇文档」都不知道。

    这张表把「文档」变成后端的一等实体：一份方案一种产物至多一份
    （`UNIQUE(solution_id, kind)`），归属经 `solution_id` 追溯到 `solutions.user_id`，
    删方案时级联删文档（`ON DELETE CASCADE`；`connect()` 里已显式打开外键）。
    """
    conn.execute(
        """
        CREATE TABLE documents (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            solution_id      INTEGER NOT NULL
                             REFERENCES solutions(id) ON DELETE CASCADE,
            kind             TEXT    NOT NULL,
            content          TEXT    NOT NULL DEFAULT '',
            content_sha      TEXT    NOT NULL DEFAULT '',
            pending_items    TEXT    NOT NULL DEFAULT '[]',
            last_run_summary TEXT,
            created_at       TEXT    NOT NULL,
            updated_at       TEXT    NOT NULL,
            UNIQUE (solution_id, kind)
        )
        """
    )
    # 刻意**不**再建 `solution_id` 单列索引：`UNIQUE(solution_id, kind)` 已经生成了
    # 一个以 solution_id 打头的索引，「按方案取文档」的查询由它覆盖，再建一个纯属冗余。


# 旧快照字段名 → 产物类型 → 旧回执字段名。
# 前端 `SessionDataV2` 用 camelCase，后端产物类型用下划线，这里是一次性映射表。
_BACKFILL_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("prdContent", "prd", "prdRunSummary"),
    ("apiDocsContent", "api_docs", "apiDocsRunSummary"),
    ("promptsContent", "prompts", "promptsRunSummary"),
)


def _backfill_documents(conn: sqlite3.Connection) -> None:
    """v2：把旧快照里的三份正文（连同各自的用量回执）搬进 `documents`。

    三处刻意选择：

    - **只复制、不删除**。旧字段仍留在 `solutions.data` 里。升级是新旧两套并存的
      过渡期，一旦发现新表有问题，回退到旧代码仍能读到原文；清理留到前端不再往
      快照里写正文之后。
    - **逐行容错**。回填读的是历史用户数据，某一行 JSON 损坏不该拦死整个服务的启动：
      跳过该行、记一条告警，其余照常。跳过的行原文仍在 `solutions.data` 里，不会丢。
    - **幂等**。`ON CONFLICT DO NOTHING`，重复启动不会重复写、也不会覆盖已有文档。
    """
    now = utc_now_text()
    scanned = written = skipped = 0

    for row in conn.execute("SELECT id, data FROM solutions").fetchall():
        scanned += 1
        solution_id = int(row["id"])
        try:
            snapshot = json.loads(row["data"])
        except (TypeError, ValueError):
            snapshot = None
        if not isinstance(snapshot, dict):
            skipped += 1
            logger.warning(
                "迁移跳过损坏的方案快照：solution_id=%s",
                solution_id,
                extra={"event": "migration.row_skipped", "solution_id": solution_id},
            )
            continue

        for content_field, kind, summary_field in _BACKFILL_FIELDS:
            content = snapshot.get(content_field)
            if not isinstance(content, str) or not content.strip():
                continue
            summary = snapshot.get(summary_field)
            summary_text = (
                json.dumps(summary, ensure_ascii=False) if isinstance(summary, dict) else None
            )
            conn.execute(
                "INSERT INTO documents "
                "(solution_id, kind, content, content_sha, pending_items, "
                " last_run_summary, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, '[]', ?, ?, ?) "
                "ON CONFLICT(solution_id, kind) DO NOTHING",
                (
                    solution_id,
                    kind,
                    content,
                    content_fingerprint(content),
                    summary_text,
                    now,
                    now,
                ),
            )
            written += 1

    logger.info(
        "文档回填完成：扫描 %d 个方案，写入 %d 份文档，跳过 %d 行",
        scanned,
        written,
        skipped,
        extra={
            "event": "migration.documents_backfilled",
            "scanned": scanned,
            "written": written,
            "skipped": skipped,
        },
    )


def _add_pending_items_to_jobs(conn: sqlite3.Connection) -> None:
    """v3：给 `generation_jobs` 加 `pending_items` 列。

    待确认清单是「一次生成」产出的第一类元数据，刷新重连时要从 DB 快照里一并恢复，
    因此任务记录也得存一份（与 `documents.pending_items` 单列同源，但职责不同：前者服务于
    「任务进行中 / 刚结束的重连快照」，后者是「已落稿文档」的权威清单）。

    设计取舍：

    - **`NOT NULL DEFAULT '[]'`**：旧任务行没有这一列，加列时若不设默认值，SQLite 对
      已有行会填上 NULL，反而要再写一次 `UPDATE` 兜底；直接给默认值更干净，且读取侧一律
      按「缺省即空清单」处理。
    - **走迁移而非改 `init_schema`**：已在 P0 约定「新表 / 新列一律走迁移」，新库也从
      版本 0 起步逐条跑，避免「新库靠 init_schema、旧库靠迁移」两处各建一次结构。
    """
    conn.execute(
        "ALTER TABLE generation_jobs ADD COLUMN pending_items TEXT NOT NULL DEFAULT '[]'"
    )


def _create_document_versions_table(conn: sqlite3.Connection) -> None:
    """v4：建 `document_versions`，并把 `documents` 改成「只留一个当前版本指针」。

    在此之前，`documents` 一行既是「文档」也是「正文」——每次生成/改写都**直接覆盖**
    同一行的 `content`，改坏了无从回头。这一步把「正文」抽成一条**版本链**：正文只存在
    `document_versions` 里，`documents` 行退化为「这份文档的身份 + 它当前指向哪一版」。

    为什么拆成两步（本步只建结构、v5 才回填、v6 才删列）：

    - `documents.current_version_id → document_versions.id`（`ON DELETE SET NULL`）与
      `document_versions.document_id → documents.id`（`ON DELETE CASCADE`）**互指**，
      是一对环。回填时必须「先空着指针插版本行、再回填指针」，否则环形约束过不去——
      所以结构先就位，数据下一小步单独搬。
    - 删列是**不可逆的单向门**（改完旧代码读不到 `documents.content`），单独成步，
      让它与「建结构 / 搬数据」这两件可反复排查的事分开。

    `metadata` 是给后续阶段留的口袋（例如 v3.1 的 `quality_gate`），本期只写入 `'{}'`。
    `pending_items` / `last_run_summary` 仍是**显式列**、不塞进口袋：它们是「一次生成」
    的第一类产物（见 ADR-023），与「附加信息」要分清主次。
    """
    conn.execute(
        """
        CREATE TABLE document_versions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id      INTEGER NOT NULL
                             REFERENCES documents(id) ON DELETE CASCADE,
            version_no       INTEGER NOT NULL,
            content          TEXT    NOT NULL DEFAULT '',
            content_sha      TEXT    NOT NULL DEFAULT '',
            pending_items    TEXT    NOT NULL DEFAULT '[]',
            last_run_summary TEXT,
            source           TEXT    NOT NULL DEFAULT 'generated',
            note             TEXT    NOT NULL DEFAULT '',
            metadata         TEXT    NOT NULL DEFAULT '{}',
            created_at       TEXT    NOT NULL,
            updated_at       TEXT    NOT NULL,
            UNIQUE (document_id, version_no)
        )
        """
    )
    # 不再另建索引：`UNIQUE(document_id, version_no)` 已生成一个以 `document_id` 打头的
    # 索引，「按文档列版本 / 取最大 version_no」都由它覆盖。
    #
    # 用 ALTER 加列而非重建 `documents`：本机 SQLite 支持 ADD COLUMN 带 REFERENCES，
    # 且**只在默认值为 NULL 时允许**——这里没有默认值即 NULL，恰好合法。
    # 也不用「关外键 + 重建表」那套：`PRAGMA foreign_keys` 在事务内是空操作，而迁移
    # 运行器把每一步包在 `BEGIN ... COMMIT` 里，步骤内部根本关不掉外键（见 §2 说明）。
    conn.execute(
        "ALTER TABLE documents ADD COLUMN current_version_id INTEGER "
        "REFERENCES document_versions(id) ON DELETE SET NULL"
    )


def _backfill_document_versions(conn: sqlite3.Connection) -> None:
    """v5：把现有 `documents` 行的正文/清单/回执搬成各自的第一条版本。

    分两步、且顺序不能反：**先插版本行拿到 id，再回填 `current_version_id`**。
    反过来（先给一个还不存在的版本 id 赋指针）会撞上环形外键。

    三处刻意选择，与 v2 回填同一套理由：

    - **逐行容错**：回填读的是历史用户数据，某一行损坏不该拦死整个服务的启动；坏行跳过、
      记一条告警，其余照常。旧正文仍留在 `documents` 列里，下一步（v6）才删。
    - **幂等**：`WHERE NOT EXISTS` 只挑「还没有任何版本」的文档行 + `ON CONFLICT DO NOTHING`
      双保险，重复启动不会重复写、也不会覆盖已有版本。
    - **原样搬运**：`content_sha` / `pending_items` / `last_run_summary` 不改形状、不重算——
      回执是历史产物，重算反而失真。`source='migrated'` 让这一条能一眼认出是回填来的。
    """
    now = utc_now_text()
    scanned = written = skipped = 0

    rows = conn.execute(
        "SELECT id, content, content_sha, pending_items, last_run_summary, "
        "       created_at, updated_at "
        "FROM documents d "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM document_versions v WHERE v.document_id = d.id"
        ")"
    ).fetchall()

    for row in rows:
        scanned += 1
        document_id = int(row["id"])
        try:
            cursor = conn.execute(
                "INSERT INTO document_versions "
                "(document_id, version_no, content, content_sha, pending_items, "
                " last_run_summary, source, note, metadata, created_at, updated_at) "
                "VALUES (?, 1, ?, ?, ?, ?, 'migrated', '', '{}', ?, ?) "
                "ON CONFLICT(document_id, version_no) DO NOTHING",
                (
                    document_id,
                    row["content"] or "",
                    row["content_sha"] or "",
                    row["pending_items"] or "[]",
                    row["last_run_summary"],
                    row["created_at"] or now,
                    row["updated_at"] or now,
                ),
            )
            version_id = int(cursor.lastrowid or 0)
            if cursor.rowcount == 0:
                # ON CONFLICT 命中时 `lastrowid` 不可靠，显式取回已存在的版本 1
                existing = conn.execute(
                    "SELECT id FROM document_versions "
                    "WHERE document_id = ? AND version_no = 1",
                    (document_id,),
                ).fetchone()
                version_id = int(existing["id"]) if existing else 0
            if version_id:
                conn.execute(
                    "UPDATE documents SET current_version_id = ? "
                    "WHERE id = ? AND current_version_id IS NULL",
                    (version_id, document_id),
                )
                written += 1
        except Exception:  # noqa: BLE001 - 回填逐行容错：坏行跳过，不拦死启动
            skipped += 1
            logger.warning(
                "迁移跳过无法回填的文档行：document_id=%s",
                document_id,
                extra={"event": "migration.version_row_skipped", "document_id": document_id},
            )

    logger.info(
        "文档版本回填完成：扫描 %d 行，写入 %d 条版本，跳过 %d 行",
        scanned,
        written,
        skipped,
        extra={
            "event": "migration.document_versions_backfilled",
            "scanned": scanned,
            "written": written,
            "skipped": skipped,
        },
    )


def _drop_documents_content_columns(conn: sqlite3.Connection) -> None:
    """v6：删掉 `documents` 上已被版本表接管的四列——**单向门**。

    删完旧代码再也读不到 `documents.content`，回滚只能靠迁移前的数据库文件备份。
    之所以敢删：正文已在上一步完整搬进 `document_versions`，且两份并存会立刻引出
    「谁是当前正文的真源」这种迟早出错的歧义。删列让「正文只有一个家」成为结构事实。

    为什么用原生 `DROP COLUMN` 而不是「重建表」：

    - 本机 SQLite ≥ 3.35，这四列都是普通列（非主键、无 UNIQUE、未进索引 / 外键 /
      CHECK / 触发器 / 视图），原生删列直接可用，不必重建。
    - 重建表那套要先 `PRAGMA foreign_keys=OFF`，而**该 PRAGMA 在事务内是空操作**，
      迁移运行器又把每一步包在事务里——步骤内部关不掉外键。为个别需求去改运行器
      不划算，所以走原生删列。

    级联链在删列后依旧成立：删方案 → 删 `documents` 行 → 删其 `document_versions`。
    """
    for column in ("content", "content_sha", "pending_items", "last_run_summary"):
        # 列名来自本文件内的固定字面量，不是外部输入，没有注入面。
        conn.execute(f"ALTER TABLE documents DROP COLUMN {column}")


def _add_content_updated_at_to_solutions(conn: sqlite3.Connection) -> None:
    """v7：给 `solutions` 加 `content_updated_at`——「最后一次**内容**变更的时间」。

    为什么需要第二根时间轴
    ----------------------
    `updated_at` 是「最后一次写库」，而这份快照里同时装了**内容**（需求摘要、对话、
    粘贴的材料）与**相位**（`viewState`、`activeJobId` 这类只是「我翻到哪一页」的量）。
    两者的变化频率差着一个数量级：翻一下进度条、切一次对话标签都会写一次库，
    于是列表里那行时间会被刷成「刚刚」，`updated_at` 排序也会把刚看过的方案顶到最前——
    而作品其实一个字都没变。

    `content_updated_at` 只在对**作品本身**发生写入时前进，列表的时间与排序都用它。
    两个时间轴并存是刻意的，各回答一个不同的问题：

    - `updated_at`：这份记录最近一次被写过是什么时候（诊断、备份口径）；
    - `content_updated_at`：这份**东西**最近一次真的变了是什么时候（给人看）。

    回填为什么取 `updated_at`
    ------------------------
    旧行没有依据可考，只能假设「最后一次写库就是最后一次改内容」——这是在**没有信息**
    时的最保守选择：它保住了旧方案之间**原有的相对顺序**。若回填成迁移时刻的同一个值，
    列表会在一夜之间变成「全部并列」，用户会以为所有方案的进度都重置了。

    `NOT NULL DEFAULT ''` 之后立刻整体 `UPDATE`，因此迁移完成后不存在空值；
    保留 `NOT NULL` 是为了让读取侧永远不必写 `or updated_at` 那种兜底分支——
    兜底分支一旦写进查询条件，排序就没法用索引了。

    索引与列都在这一步建：列也是这一步加的，没有更早的时机。新库同样从版本 0 起步、
    逐条跑迁移，所以不必（也不该）在 `init_schema` 里再写一遍——两处各建一次迟早就对不上。
    """
    conn.execute(
        "ALTER TABLE solutions ADD COLUMN content_updated_at TEXT NOT NULL DEFAULT ''"
    )
    # 刚加出来的列全是空串，这里一次性按「最后一次写库即最后一次改内容」回填。
    conn.execute("UPDATE solutions SET content_updated_at = updated_at")
    # 列表查询是「owner + 按内容时间倒序」，复合索引同时覆盖过滤与排序。
    # 旧索引 `idx_solutions_owner(user_id, updated_at DESC)` 保留：`updated_at` 仍在写，
    # 且「最近打开」这类查询将来若要回来，它还在。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_solutions_owner_content "
        "ON solutions(user_id, content_updated_at DESC)"
    )


def _add_doc_kind_to_jobs(conn: sqlite3.Connection) -> None:
    """v8：给 `generation_jobs` 加 `doc_kind`——这次任务**写的是哪一份产物**。

    为什么需要单独一列
    ------------------
    `kind` 回答的是「做了什么」（`prd` / `api_docs` / `prompts` / `optimize` /
    `clarify` / `auto_clarify`），但对后三个动作**定不了产物**：「优化」既可以优化 PRD，
    也可以优化提示词。产物类型原本只存在于**请求体**里，落库时才由
    `_target_product_kind(kind, request)` 现场推导——于是刷新 / 进程重启之后，
    任何只拿得到数据库行的路径（DB 兜底快照、`GET /api/jobs`）都无法回答
    「这个 optimize 任务改的是哪一份」。

    前端的代价最直观：重连时只能靠「当前停在哪个审阅页」去猜，猜不到就 `?? 'prd'`
    兜底——**猜错就是拿优化接口文档的结果覆盖 PRD 正文**。把目标产物落成列，
    归属就变成一条可查的事实，而不是一次推断。

    设计取舍
    --------
    - **创建时就写**：`_build_request` 在创建接口里已经拿到完整请求体，
      `_target_product_kind` 是纯请求推导，不依赖入口模式或前端快照，没有「创建时算不出来」
      的问题。晚算（收尾时）只是历史写法。
    - **`NOT NULL DEFAULT ''`**：旧行没有依据可考，空串表示「未知」；读取侧统一
      `or None`。不用可空列是为了避免 SQLite 对已有行填 NULL 之后还要再 `UPDATE` 一次。
    - 空串**不等于** `prd`：不知道就不猜，与 `_document_kind` 的既有口径一致。
    """
    conn.execute("ALTER TABLE generation_jobs ADD COLUMN doc_kind TEXT NOT NULL DEFAULT ''")


# 方案状态从 `data` 拆出来的列。
#
# 为什么拆
# --------
# `data` 是一整份 `SessionDataV2` 的 JSON。它里面同时住着三类东西：
# **相位**（翻到哪一页、指着哪个任务）、**草稿**（还没提交的录入内容）、
# **记录**（聊天）。前两类只有前端一个写者，放进一个 JSON 是好选择——一次
# `UPSERT` 原子生效，加字段也不必写迁移。
#
# 但「一个写者」这个前提在会话被多处访问之后不成立了：两台设备、切方案回切，
# 都在整份覆盖这一列，**后写的把先写的整份盖掉**。而 `messages` 更是只增不减，
# 想按线程查、想按线程拼历史，都不能靠解析一个不透明字符串。
#
# 于是拆成三层，各自按自己的读写模式落地：
# - 相位与草稿 → 拆成本表里的标量列（本步）；
# - 聊天 → `chat_turns`（一句话一行，只追加）；
# - 后端会话 id → `chat_sessions`（按方案 + 线程唯一）。
#
# `summary` 刻意**不拆成列**而留一列 JSON：它是张问卷，加一个问题就加一个字段，
# 拆成列等于每次改问卷都要写迁移。演进快的结构放 JSON，演进慢的建列。
_SOLUTION_STATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("view_state", "TEXT NOT NULL DEFAULT ''"),
    ("entry_mode", "TEXT NOT NULL DEFAULT ''"),
    ("chat_tab", "TEXT NOT NULL DEFAULT ''"),
    ("summary_json", "TEXT NOT NULL DEFAULT ''"),
    ("quick_product_name", "TEXT NOT NULL DEFAULT ''"),
    ("quick_requirements", "TEXT NOT NULL DEFAULT ''"),
    ("draft_prd_text", "TEXT NOT NULL DEFAULT ''"),
    ("paste_prd_text", "TEXT NOT NULL DEFAULT ''"),
    ("paste_api_docs_text", "TEXT NOT NULL DEFAULT ''"),
    ("active_job_kind", "TEXT NOT NULL DEFAULT ''"),
    ("active_job_doc_kind", "TEXT NOT NULL DEFAULT ''"),
    # 指针本身可空：「没有任务」与「任务 id 是 0」是两件事，不给它编默认值
    ("active_job_id", "INTEGER"),
)


def _create_chat_tables(conn: sqlite3.Connection) -> None:
    """v9：方案状态拆成列；新建 `chat_turns` 与 `chat_sessions`。

    `chat_turns` 的三个关键设计
    --------------------------
    - **按 `id` 排序，不建 `seq` 列**：主键自增天然单调。排序键存在的唯一理由是
      「把一条插到两条中间」，而同一线程同时只允许一轮在跑（发起端已拦），
      轮次永远是追加，不需要插入。为不存在的功能养一列，是负债。
    - **`role` 含 `tool`**：结构化选择题的作答在上游 API 里必须是 `ToolMessage`，
      否则历史里留下悬空的 `tool_calls`，调用直接失败。前端拿不到这个角色，
      由读接口投影成用户气泡。
    - **`(job_id, role)` 唯一**：一个对话任务恰好产出两条轮次（进去的一条 +
      出来的一条）。把幂等做成结构事实之后，重连两次、重试两次、收尾撞车，
      都不可能多写一条——不必指望每条代码路径都记得检查。
      迁移来的旧数据 `job_id` 为空，被部分索引排除在外。

    `data` 列保留但**不再写也不再读**：它是旧快照的最后一份副本，留着当历史备份；
    新行写空串（该列是 `NOT NULL`），运行期没有任何代码读它。
    """
    for column, ddl in _SOLUTION_STATE_COLUMNS:
        conn.execute(f"ALTER TABLE solutions ADD COLUMN {column} {ddl}")


    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_turns (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            solution_id  INTEGER NOT NULL REFERENCES solutions(id) ON DELETE CASCADE,
            stage        TEXT    NOT NULL,
            role         TEXT    NOT NULL,
            content      TEXT    NOT NULL DEFAULT '',
            tool_call_id TEXT    NOT NULL DEFAULT '',
            tool_result  TEXT    NOT NULL DEFAULT '',
            asks         TEXT    NOT NULL DEFAULT '[]',
            answers      TEXT    NOT NULL DEFAULT '[]',
            tool_calls   TEXT    NOT NULL DEFAULT '[]',
            interrupted  INTEGER NOT NULL DEFAULT 0,
            job_id       INTEGER,
            created_at   TEXT    NOT NULL
        )
        """
    )
    # 读线程（按方案 + 线程取全部轮次，按 id 排序）与拼历史（取某一轮之前的）都走它
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_turns_thread "
        "ON chat_turns(solution_id, stage, id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_turns_job_role "
        "ON chat_turns(job_id, role) WHERE job_id IS NOT NULL"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            solution_id     INTEGER NOT NULL REFERENCES solutions(id) ON DELETE CASCADE,
            stage           TEXT    NOT NULL,
            conversation_id TEXT    NOT NULL DEFAULT '',
            updated_at      TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_sessions_thread "
        "ON chat_sessions(solution_id, stage)"
    )


def _backfill_solution_state(conn: sqlite3.Connection) -> None:
    """v10：把存量 `solutions.data` 里的状态搬进新列与新表。

    逐行容错
    --------
    回填读的是旧的、不受本服务控制的用户数据，**某一行损坏不该拦死整个服务的启动**。
    解析失败的行原样跳过并记一条 warning：`data` 里的 JSON 仍在，随时可以人工复查，
    而「启动失败」会让所有人都用不了。

    `messages` 怎么落
    -----------------
    - 角色原样保留（`user` / `assistant` / `system`）；
    - `stage` 缺失的一律归需求澄清——那是旧快照（还没有线程概念）的年代；
    - `job_id` 留空：这些轮次不是任何一次任务产出的，编一个 id 只会让
      `(job_id, role)` 唯一约束在将来撞车；
    - `asks` / `answers` / `tool_calls` 原样搬进 JSON 列，顺序由 `id` 保证。

    幂等
    ----
    只在方案还没有任何 `chat_turns` 时导入。迁移本身只会跑一次，但「跑一次」的前提
    不该是唯一保障——重跑一次不该把聊天记录翻倍。
    """
    rows = conn.execute("SELECT id, data FROM solutions").fetchall()
    migrated_turns = 0
    migrated_sessions = 0
    skipped = 0

    for solution_id, raw in rows:
        if not raw:
            continue
        try:
            snapshot = json.loads(raw)
        except (TypeError, ValueError):
            skipped += 1
            logger.warning(
                "方案快照不是合法 JSON，跳过回填（data 原样保留）",
                extra={"event": "migration.solution_snapshot_invalid", "solution_id": solution_id},
            )
            continue
        if not isinstance(snapshot, dict):
            skipped += 1
            logger.warning(
                "方案快照不是对象，跳过回填（data 原样保留）",
                extra={"event": "migration.solution_snapshot_not_object", "solution_id": solution_id},
            )
            continue

        summary = snapshot.get("summary")
        summary_json = (
            json.dumps(summary, ensure_ascii=False) if isinstance(summary, dict) else ""
        )
        quick = snapshot.get("quickInput")
        quick = quick if isinstance(quick, dict) else {}
        paste = snapshot.get("pastePairInput")
        paste = paste if isinstance(paste, dict) else {}

        conn.execute(
            "UPDATE solutions SET view_state = ?, entry_mode = ?, chat_tab = ?, "
            "summary_json = ?, quick_product_name = ?, quick_requirements = ?, "
            "draft_prd_text = ?, paste_prd_text = ?, paste_api_docs_text = ?, "
            "active_job_id = ?, active_job_kind = ?, active_job_doc_kind = ? "
            "WHERE id = ?",
            (
                str(snapshot.get("viewState") or ""),
                str(snapshot.get("entryMode") or ""),
                str(snapshot.get("chatTab") or ""),
                summary_json,
                str(quick.get("productName") or ""),
                str(quick.get("requirements") or ""),
                str(snapshot.get("debugPrdText") or ""),
                str(paste.get("prd") or ""),
                str(paste.get("apiDocs") or ""),
                snapshot.get("activeJobId") if isinstance(snapshot.get("activeJobId"), int) else None,
                str(snapshot.get("activeJobKind") or ""),
                str(snapshot.get("activeJobDocKind") or ""),
                solution_id,
            ),
        )

        owner = conn.execute(
            "SELECT user_id FROM solutions WHERE id = ?", (solution_id,)
        ).fetchone()
        owner_id = int(owner[0]) if owner else 0

        existing = conn.execute(
            "SELECT 1 FROM chat_turns WHERE solution_id = ? LIMIT 1", (solution_id,)
        ).fetchone()
        if not existing:
            messages = snapshot.get("messages")
            for message in messages if isinstance(messages, list) else []:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "")
                if not is_valid_role(role):
                    continue
                asks = message.get("asks")
                answers = message.get("answers")
                tool_calls = message.get("tool_calls")
                conn.execute(
                    "INSERT INTO chat_turns (user_id, solution_id, stage, role, content, "
                    "tool_call_id, tool_result, asks, answers, tool_calls, interrupted, "
                    "job_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, 0, NULL, ?)",
                    (
                        owner_id,
                        solution_id,
                        normalize_stage(message.get("stage")),
                        role,
                        str(message.get("content") or ""),
                        json.dumps(asks, ensure_ascii=False) if isinstance(asks, list) else "[]",
                        json.dumps(answers, ensure_ascii=False)
                        if isinstance(answers, list)
                        else "[]",
                        json.dumps(tool_calls, ensure_ascii=False)
                        if isinstance(tool_calls, list)
                        else "[]",
                        str(message.get("created_at") or "") or utc_now_text(),
                    ),
                )
                migrated_turns += 1

        conversation_ids = snapshot.get("conversationIds")
        if isinstance(conversation_ids, dict):
            now = utc_now_text()
            for stage, conversation_id in conversation_ids.items():
                if not is_valid_stage(stage) or not conversation_id:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO chat_sessions "
                    "(user_id, solution_id, stage, conversation_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (owner_id, solution_id, stage, str(conversation_id), now),
                )
                migrated_sessions += 1

    logger.info(
        "方案快照回填完成",
        extra={
            "event": "migration.solution_state_backfilled",
            "rows": len(rows),
            "turns": migrated_turns,
            "sessions": migrated_sessions,
            "skipped": skipped,
        },
    )


def _add_chat_stage_to_jobs(conn: sqlite3.Connection) -> None:
    """v11：给 `generation_jobs` 加 `chat_stage`——这一轮对话属于哪条线程。

    为什么是**单独一步**而不是塞回 v9
    --------------------------------
    v9 建表时漏了这一列，而它已经被某些库应用过。迁移是按 `user_version` 跳过的，
    往一个已应用的步骤里加 DDL，在那些库上**永远不会执行**——表现为「新库一切正常、
    开发机上却报 `no such column: chat_stage`」，而且报错发生在运行期、离原因很远。

    这正是本模块开头那条约定（历史步骤不可改）要防的事，这次踩了一次。
    教训：**结构变更只能追加新版本号**，哪怕改动看起来只隔了几分钟。

    这里先查一次列再决定加不加：新库会在 v9 之后顺序跑到本步，而已经手工补过列的库
    也不该因为「列已存在」而启动失败。幂等靠事实判断，不靠版本号猜。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_jobs)")}
    if "chat_stage" in columns:
        return
    conn.execute(
        "ALTER TABLE generation_jobs ADD COLUMN chat_stage TEXT NOT NULL DEFAULT ''"
    )


def _add_is_admin_to_users(conn: sqlite3.Connection) -> None:
    """v12：给 `users` 加 `is_admin`——这个人是不是**平台管理员**。

    为什么需要它
    ------------
    v4 管理员后台的地基，是让后端能**自己**区分普通用户与管理员，从而在每个
    管理员接口前独立校验。在此之前用户表只有「是否启用」，没有任何权限维度：
    `INIT_ADMIN_*` 建出来的首个账号与普通账号在数据上完全等价，于是
    「他是不是管理员」只能靠前端藏入口——而那等于把锁装在门面上（ADR-039）。

    **默认值 0 的语义很重要**：存量账号一律不是管理员。宁可让运维显式任命
    （`scripts/auth_cli.py promote`），也不能让一次升级把现有用户集体提权——
    后者是「悄悄扩大了权限面」，而且没有任何报错。

    与 v11 同样先查列再决定加不加：已经手工补过列的库不该因为「列已存在」
    而启动失败，幂等靠事实判断而不是版本号猜。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "is_admin" in columns:
        return
    conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")


# 迁移清单。顺序即执行顺序。新增结构一律追加到末尾，**不要修改历史步骤**。
# 每一版的内容请在该步骤的 docstring 里写清「为什么需要它、对旧数据意味着什么」。
MIGRATIONS: list[Migration] = [
    Migration(version=1, name="documents_table", apply=_create_documents_table),
    Migration(version=2, name="backfill_documents", apply=_backfill_documents),
    Migration(version=3, name="jobs_pending_items", apply=_add_pending_items_to_jobs),
    Migration(version=4, name="document_versions_table", apply=_create_document_versions_table),
    Migration(version=5, name="backfill_document_versions", apply=_backfill_document_versions),
    Migration(version=6, name="documents_drop_content_columns", apply=_drop_documents_content_columns),
    Migration(
        version=7,
        name="solutions_content_updated_at",
        apply=_add_content_updated_at_to_solutions,
    ),
    Migration(version=8, name="jobs_doc_kind", apply=_add_doc_kind_to_jobs),
    Migration(version=9, name="solutions_state_and_chat_tables", apply=_create_chat_tables),
    Migration(version=10, name="backfill_solution_state", apply=_backfill_solution_state),
    Migration(version=11, name="jobs_chat_stage", apply=_add_chat_stage_to_jobs),
    Migration(version=12, name="users_is_admin", apply=_add_is_admin_to_users),
]


def current_version(conn: sqlite3.Connection) -> int:
    """读取库里记录的 schema 版本；`0` 表示「只有 `init_schema` 的基线」。"""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _validate_plan(plan: list[Migration]) -> None:
    """校验迁移清单本身是合法的：版本号从 1 开始、严格连续递增。

    这个检查放在启动路径上，是为了让「漏写了版本号 / 顺序写反了」在**添加步骤的
    那一刻**就炸出来，而不是等到某个库升级到一半才出现诡异行为。
    """
    expected = 1
    for migration in plan:
        if migration.version != expected:
            raise ValueError(
                f"迁移清单版本号必须从 1 开始连续递增：期望 {expected}，"
                f"实际 {migration.version}（{migration.name}）"
            )
        expected += 1


def run_migrations() -> int:
    """把所有尚未应用的迁移依次跑掉，返回最终版本号。

    Raises:
        ValueError: 迁移清单本身不合法（版本号不连续）。
        Exception: 某一步执行失败——原样抛出，由调用方（lifespan）中止启动。
            已成功的步骤不会回滚：`user_version` 已推进到那一档，重跑会从断点继续。
    """
    plan = MIGRATIONS
    _validate_plan(plan)

    with connect() as conn:
        applied = current_version(conn)
        if applied > len(plan):
            # 库里的版本比本代码认识的还新：多半是用旧代码配新库。不擅自降级，
            # 但要让人一眼看到「这个进程并不完全认识当前的库」。
            logger.warning(
                "数据库 schema 版本（%d）高于本代码已知的迁移数（%d），将不做任何迁移",
                applied,
                len(plan),
                extra={"event": "migration.db_newer", "db_version": applied},
            )
        for migration in plan:
            if migration.version <= applied:
                continue
            logger.info(
                "应用迁移 v%d %s",
                migration.version,
                migration.name,
                extra={"event": "migration.applying", "version": migration.version},
            )
            # 每一步放进显式事务。SQLite 的 DDL 是可回滚的，失败时能把「建了一半的
            # 表」一并撤掉；否则重跑会撞上「表已存在」，把一个可自愈的失败变成死结。
            conn.execute("BEGIN")
            try:
                migration.apply(conn)
                # PRAGMA 不支持参数绑定，只能拼字符串；这里的值来自我们自己的
                # 清单（int），不是外部输入，因此没有注入面。
                conn.execute(f"PRAGMA user_version = {migration.version:d}")
            except Exception:
                conn.rollback()
                raise
            conn.commit()
            applied = migration.version

    return applied
