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
- **新表既要进迁移、也要进 `init_schema` 的 `SCHEMA_SQL`**（两边写同一份 DDL）。
  只加迁移 → 只跑 `init_schema` 的测试与脚本会少表（`tests/conftest.py` 的
  `isolated_db` 就只调 `init_schema()`）；只改基线 → 旧库永远没有这张表。
  这条约定曾经反过来写过（「新表不进 SCHEMA_SQL」），v13 起被实际做法取代，
  与 `core/db.py` 的模块注释对齐。

失败处理的分野
--------------
- **结构变更（DDL）失败 → 抛出异常、中止启动**：带着半个 schema 起服务，只会把
  问题拖到运行期，而且更难定位。启动失败是这里最便宜的失败方式。
- **数据回填失败 → 逐行容错**：回填读的是旧的、不受本服务控制的用户数据，
  某一行损坏不该拦死整个服务的启动。具体策略由各步骤自己实现。

数据清理型迁移（与结构变更不同，多一条要求）
------------------------------------------
绝大多数步骤只加表 / 加列，删错了也还能重建。**「清理数据」的步骤删完就没了**，
所以它必须做到：

1. **判据能窄就窄**，且每一条都要写成 SQL 里的显式条件——「用户从来没动过这条」
   这种判断要用 `created_at = updated_at` 这类**事实**，而不是猜。
2. **永不删「正在生效的那一条」**。所谓「出厂默认」里往往总有用户正在用的那一行，
   把它一起清掉的结果是「升级完列表空了、生成也挂了」，而迁移本身不会报错。
   （v19 `provider_config_entries` 就是靠多一个「不是当前使用中」的条件兜住的。）
3. **配一条「该活下来的活下来了」的用例**，而不只是断言「该删的删了」。
   只测删的话，判据写宽了照样绿。
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


def _create_app_logs_table(conn: sqlite3.Connection) -> None:
    """v13：建 `app_logs`——让日志第一次被**留下来**。

    为什么需要它
    ------------
    在此之前日志只装了一个 `StreamHandler(sys.stdout)`：**进程一停，历史全没**。
    管理员后台要「查看日志」，前提是先有一条采集链路，所以它属于地基，
    而且必须**尽早**挂上——日志是「从现在开始攒」的数据，等到做 UI 时才采，
    上线那天看到的就是空列表（ADR-040）。

    ⚠️ 关于版本号：`docs/architecture/v4/roadmap.md` 初稿里把它排成了 v15，
    但迁移清单要求版本号严格连续，而它实际是 v12 之后**第一个**落地的新结构，
    故正式编号为 v13（原 v13 Token 列、v14 审计表顺延为 v14、v15）。
    文档已同步。**编号只用于排序，不是需求优先级**。

    这里只建表不建数据：表里的内容由 `core/logging_setup.py` 挂的 `LogStoreHandler`
    实时写入。这是个**可随时清空重建**的派生表，不参与业务约束，故不加外键——
    业务表删了不应牵连日志，日志也不该拦住任何业务删除。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL,
            level      TEXT    NOT NULL,
            logger     TEXT    NOT NULL,
            msg        TEXT    NOT NULL,
            request_id TEXT,
            user_id    INTEGER,
            event      TEXT,
            extra_json TEXT    NOT NULL DEFAULT '',
            exc        TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_logs_ts      ON app_logs(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_logs_level   ON app_logs(level, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_logs_request ON app_logs(request_id)")


def _add_token_columns_to_jobs(conn: sqlite3.Connection) -> None:
    """v14：给 `generation_jobs` 加 `prompt_tokens` / `completion_tokens` 两列。

    为什么需要它
    ------------
    用量此前只存在于 `usage` 这一列 JSON 里。要回答「这个用户一共烧了多少 token」、
    「平台今天消耗多少」，就得把**每一行**取回 Python 解析一遍——数据量一大就跑不动，
    而且 JSON 结构一变聚合就静默失真。

    加两列后，`SUM(prompt_tokens)` 是一条 SQL 的事。

    它们是**派生副本**，不与 JSON 竞争真源：展示明细仍读 `usage`（含分步、耗时），
    聚合只读这两列。副本写错不影响正确性判断，因为真源还在；而解析 JSON 出错
    则连真源都读不出来——这个方向的选择是有意的。

    存量行为 0：旧任务没有这两列可填，落到 0。宁可少算，也不猜一个数字填进去。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_jobs)")}
    if "prompt_tokens" not in columns:
        conn.execute(
            "ALTER TABLE generation_jobs ADD COLUMN prompt_tokens INTEGER NOT NULL DEFAULT 0"
        )
    if "completion_tokens" not in columns:
        conn.execute(
            "ALTER TABLE generation_jobs ADD COLUMN completion_tokens INTEGER NOT NULL DEFAULT 0"
        )


def _create_admin_audit_log_table(conn: sqlite3.Connection) -> None:
    """v15：建 `admin_audit_log`——管理员每一次写操作留痕。

    为什么需要它
    ------------
    管理员后台能删账号、改配置、发邀请码。这些操作的破坏力远大于普通用户动作，
    而「谁在什么时候改了什么」如果无处可查，出事之后只能靠回忆。

    与 `app_logs` 的分工
    --------------------
    `app_logs` 是**系统**在说什么（排障用，可随时清空）；
    这里是**人**做了什么（问责用，不该随便清）。两者混在一张表里，
    排障时会被大量人为操作淹没，追责时又会漏在日志风暴里。

    刻意**不加外键**：管理员账号被删之后，他做过的操作仍要看得见。
    设了外键就会随删除一起消失——而那恰恰是最需要保留的那些记录。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id   INTEGER,
            actor_name TEXT    NOT NULL DEFAULT '',
            action     TEXT    NOT NULL,
            target     TEXT    NOT NULL DEFAULT '',
            detail     TEXT    NOT NULL DEFAULT '',
            request_id TEXT,
            ts         TEXT    NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_ts    ON admin_audit_log(ts)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_actor ON admin_audit_log(actor_id, ts)"
    )


def _create_runtime_config_tables(conn: sqlite3.Connection) -> None:
    """v16：建 `model_providers` 与 `system_config`，并播种六家内置供应商。

    为什么需要它们
    --------------
    在此之前所有配置只在 `.env` 里：改一个值要改文件、重启服务。后台因此无法
    「调参数」，供应商也无法切换（切换要改代码里的 Literal 与分支）。

    两张表分工
    ----------
    - `system_config`：**只存覆盖项**。没有某一行 = 用 env 或代码默认值。
      这是「升级后行为零变化」的结构保证——迁移不写任何覆盖行，所以所有键
      仍然取 env 的现有值；「恢复默认」= 删掉那一行。
    - `model_providers`：供应商登记。密钥**只存环境变量名**（ADR-042），
      不落明文；激活真源是 `system_config.active_provider_id`，**不在本表加
      `is_active` 列**（否则可能出现两行都为 1 的矛盾状态）。

    为什么 `protocol` 而不是厂商名
    ------------------------------
    LangChain 的 chat 集成有二十来家，但按协议只有六类（见 `core/providers.py`）。
    按协议建模，加一家 OpenAI 兼容的新厂商只需填 `base_url` + 密钥变量名，
    **不改代码、不加依赖**；只有换协议才需要加适配分支。

    为什么播种「六家」而不是「当前那一家」（历史，已由 v19 收回）
    --------------------------------------------------------------
    当时的想法是「后台的供应商页一进去就该看到全部可选协议」，播种只插内置行、
    不激活任何一家：没配 `active_provider_id` 时跟随 `DEFAULT_LLM_PROVIDER`。

    这个假设后来被真实使用打破：六行里五行是用户永远不用的，列表被它们占满，
    「哪些是我真配的」反而看不出来。所以 **v19 把这六行清掉了**，
    本步骤在新库上播下的行随即被删；本函数保留原样只是因为老库不会重跑它。

    `INSERT OR IGNORE` 的用意
    -------------------------
    迁移可能被重跑（旧库补升、或在已升过的库上再跑一次）。用 `OR IGNORE`
    保证「已存在的 id 一律不覆盖」——管理员改过的地址、密钥变量名、模型名
    不会被一次迁移重置回默认值。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_providers (
            id                TEXT PRIMARY KEY,
            name              TEXT    NOT NULL DEFAULT '',
            protocol          TEXT    NOT NULL DEFAULT 'openai_compatible',
            base_url          TEXT    NOT NULL DEFAULT '',
            api_key_env_var   TEXT    NOT NULL DEFAULT '',
            default_model     TEXT    NOT NULL DEFAULT '',
            max_tokens        INTEGER NOT NULL DEFAULT 4096,
            context_window    INTEGER NOT NULL DEFAULT 0,
            max_output        INTEGER NOT NULL DEFAULT 0,
            thinking_enabled  INTEGER NOT NULL DEFAULT 0,
            extra_json        TEXT    NOT NULL DEFAULT '{}',
            builtin           INTEGER NOT NULL DEFAULT 0,
            enabled           INTEGER NOT NULL DEFAULT 1,
            sort_order        INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT    NOT NULL,
            updated_at        TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_config (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by INTEGER
        )
        """
    )

    # ⚠️ **这里不再读 `Settings`**（v19 起，模型接入已整体迁入后台面板，ADR-051）。
    # 但本步骤仍必须原样播种这六行：v16 早已在所有既有库上执行过，改它等于没改；
    # 而在**全新库**上它仍会跑一次，播下的行随即被 v19 删除（判据见 v19）。
    # 所以这里给的是**固定的历史形状**，值只求「能插进去」——
    # 它们的最终归宿是 `DELETE`，不是被读取。
    #
    # 用常量而不是「干脆不播种」：那样新库与老库的中间状态就分叉了，
    # 而 `INSERT OR IGNORE` 的存在意义（重跑不覆盖管理员改过的值）也会失去验证场景。
    now = utc_now_text()

    seeds: list[tuple[str, str, str, str, str, str, int, int]] = [
        (
            "ark",
            "火山方舟",
            "ark",
            "https://ark.cn-beijing.volces.com/api/v3",
            "ARK_API_KEY",
            "",
            4096,
            0,
        ),
        ("anthropic", "Anthropic Claude", "anthropic", "", "ANTHROPIC_API_KEY", "", 4096, 0),
        ("openai", "OpenAI", "openai_compatible", "", "OPENAI_API_KEY", "", 4096, 0),
        # 后三家是新协议：只给密钥变量名与地址，**模型名留空**让管理员填。
        # 写死型号会过时；而「猜一个型号」比「空着并提示」更容易误导。
        ("gemini", "Google Gemini", "google_genai", "", "GOOGLE_API_KEY", "", 8192, 0),
        (
            "nvidia",
            "NVIDIA NIM",
            "nvidia",
            "https://integrate.api.nvidia.com/v1",
            "NVIDIA_API_KEY",
            "",
            4096,
            0,
        ),
        ("huggingface", "Hugging Face", "huggingface", "", "HUGGINGFACEHUB_API_TOKEN", "", 4096, 0),
    ]

    # 新库基线（v20 起）直接是 value_token，没有 api_key_env_var 列；而这些出厂行
    # 随后会被 v19 全部删掉，所以新库上压根不需要播种。不加这个守卫，v16 会在
    # 全新数据库上因「无 api_key_env_var 列」直接报错。老库仍有该列，行为不变。
    columns = {row[1] for row in conn.execute("PRAGMA table_info(model_providers)")}
    # 新基线（v20 起）直接用 value_token，老库仍是 api_key_env_var。出厂行一律
    # 不带密钥值，且 v19 会把这些行全部删掉，所以这里只求「插得进去、不报错」。
    key_col = "api_key_env_var" if "api_key_env_var" in columns else "value_token"

    conn.executemany(
        f"INSERT OR IGNORE INTO model_providers "
        f"(id, name, protocol, base_url, {key_col}, default_model, max_tokens, "
        " thinking_enabled, extra_json, builtin, enabled, sort_order, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', 1, 1, ?, ?, ?)",
        [
            (
                pid,
                name,
                protocol,
                base_url,
                key_var if key_col == "api_key_env_var" else "",
                model,
                max_tokens,
                thinking,
                index,
                now,
                now,
            )
            for index, (
                pid,
                name,
                protocol,
                base_url,
                key_var,
                model,
                max_tokens,
                thinking,
            ) in enumerate(seeds)
        ],
    )


def _add_quota_columns_to_users(conn: sqlite3.Connection) -> None:
    """v17：给 `users` 加三个**每日额度**覆盖列。

    为什么需要它
    ------------
    v4 之前任何人都能无限次生成：邀请码只管「谁能进来」，不管「进来后用多少」。
    一个账号跑一百份 PRD 就能把预算烧掉一大截，而管理员在事前没有任何拦的手段
    ——T2 的统计是事后的账单，它告诉你花了多少，但阻止不了继续花。

    三列的三态语义
    --------------
    `-1`（默认）**跟随全局**：用 `system_config` 里的 `quota_*` 默认值。
    `0`          **不限**：给个别可信账号开口子，或给管理员自己开。
    `N > 0`      **该用户的具体上限**。

    为什么用 `-1` 而不是 NULL 表达「跟随全局」
    ------------------------------------------
    `ALTER TABLE ADD COLUMN` 配 `NOT NULL DEFAULT -1` 对存量行是**零成本**的
    （SQLite 只改元数据）；用 NULL 则要么允许 nullable（三态变成四态，读的时候
    永远要防 None）、要么分两步重建表。而「-1 表示未设置」这个约定只活在本模块
    与 `quota_service.resolve_limits` 两处，范围足够小。

    存量数据意味着什么
    ------------------
    全部是 `-1`，即全部跟随全局；而全局默认是 `0`（不限）。
    所以**升级后行为完全不变**——这一点由 `test_quota.py` 的「未配置时零变化」钉住。

    与 v12 同样先查列再决定加不加：手工补过列的库不该因为「列已存在」而启动失败。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for name in ("daily_task_limit", "daily_token_limit", "max_running"):
        if name in columns:
            continue
        conn.execute(f"ALTER TABLE users ADD COLUMN {name} INTEGER NOT NULL DEFAULT -1")


def _create_login_failures_table(conn: sqlite3.Connection) -> None:
    """v18：`login_failures` 表——登录失败计数，用于渐进延时。

    为什么需要它
    ------------
    v4 之前 `auth_service.login` 只做了两件事：统一报错文案、用户不存在时也跑一次
    等价的 scrypt 运算。这两件都是**防账号枚举**的手段，对「反复猜密码」毫无作用——
    攻击者可以每秒试几十次，直到猜中为止。会话、邀请码、配额都管不到这里，
    因为这条路径发生在登录之前。

    为什么只存失败
    --------------
    计数要的是「最近失败了几次」。成功登录时直接删掉该用户名的记录，
    表里就只剩「尚未清账的失败」，语义与「连续失败次数」完全一致，
    不必再存一个 `ok` 列再由查询去过滤（多一列就多一种写错的可能）。

    ⚠️ 用户名按**小写归一**后存
    --------------------------
    `users.username` 是大小写敏感的（`get_by_username` 精确匹配），所以 `Root` 与 `root`
    是两个不同的查询键。若计数也按原始大小写存，攻击者只要在 `admin` / `Admin` / `ADMIN`
    之间轮换，每次都是「新的一轮」，延时形同不存在。归一之后它们共用同一个计数器。
    真正的用户查不到 `Root` 本来也会失败，所以这不影响任何合法登录。

    ⚠️ `ip` 列本期**只记录、不参与判定**
    ------------------------------------
    本期只做账号维度的渐进延时（不做 IP 维度），但把来源 IP 记下来：一是事后排查
    「这次爆破来自哪里」时不用再从访问日志里反查，二是将来真要加 IP 维度时
    不必为补一列再走一次迁移（迁移只能追加，加列要新版本号）。

    存量数据意味着什么
    ------------------
    新表，无存量。未登录过的库是空表，`count_failures` 恒为 0 → 升级后行为完全不变。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_failures (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT    NOT NULL,
            ip         TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL
        )
        """
    )
    # 计数只查「某用户名在某时刻之后」，复合索引正好覆盖过滤与排序
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_failures_user "
        "ON login_failures(username, created_at DESC)"
    )
    # 清理按时间扫全表（没有用户名条件），单独一个索引避免退化成全表扫描
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_failures_created "
        "ON login_failures(created_at)"
    )


# v16 播种过的六条内置供应商行。v19 只认这六个 id——即使将来有人手工插入
# `builtin = 1` 的行，也不会被误当成出厂数据删掉。
_V16_SEED_PROVIDER_IDS: tuple[str, ...] = (
    "ark",
    "anthropic",
    "openai",
    "gemini",
    "nvidia",
    "huggingface",
)

# 任务行上的供应商快照列（v19）。与 `core/db.py` 的 `SCHEMA_SQL` 双写：
# 新库由基线建好，旧库靠这里 `ALTER` 补上（`PRAGMA table_info` 守卫防重跑）。
_JOB_PROVIDER_SNAPSHOT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("provider_protocol", "TEXT NOT NULL DEFAULT ''"),
    ("provider_config_id", "TEXT NOT NULL DEFAULT ''"),
    ("provider_name", "TEXT NOT NULL DEFAULT ''"),
    ("provider_model", "TEXT NOT NULL DEFAULT ''"),
)


def _provider_config_entries(conn: sqlite3.Connection) -> None:
    """v19：供应商从「一协议一固定槽位」改为「配置条目列表」，并给任务留供应商快照。

    为什么要改
    ----------
    v16 把「每个协议一行」直接播进 `model_providers`，于是 `id` 既是行身份、又是
    协议名，两者绑死：想加第二条方舟配置只能手填一个不冲突的 id，而界面上那五张
    空卡片永远占着位置（见 ADR-050）。改成配置条目之后，`id` 由后端生成
    （`cfg_xxx`），同一协议可以有多条，预设退回「表单模板」、不再入库。

    这一版做三件事
    --------------
    1. **给 `generation_jobs` 加四个供应商快照列**，并从历史 `usage` JSON 回填。
       存快照而不是存 config_id 再回查：配置会被删、会被改，回查等于让历史账单
       跟着今天的状态变。
    2. **删掉「出厂空壳」**：v16 播种、且从建表起没被后台改过、且当前没在用的行。
    3. **把残留的 `builtin` 归零**：改过的内置行是同一条真实配置，它的地址与密钥
       变量名是管理员填的，不能删；但 `builtin` 这个「不可删」标记已经没有意义了
       （所有条目一视同仁，只有「使用中」的不能直删）。
       列本身保留不删：SQLite 的 `DROP COLUMN` 是单向门（v6 已经有教训）。

    ⚠️ 删除条件是四重与，缺一不可
    ---------------------------
    `builtin = 1` **且** `created_at = updated_at`（没被 `update()` 碰过，每次
    更新都会刷新 `updated_at`）**且** id 属于 v16 那六个 **且** 不是当前激活项。

    最后一条是**硬约束，不是优化**：现网正是「只有 ark 一条在跑」，它同样满足前三条。
    若把它也当出厂态删掉，后台列表会变成空的、只能靠 `env:ark` 兜底——在管理员眼里
    等于「迁移把我的配置弄丢了」。保留它（`builtin` 置 0）之后，升级完成的列表
    正好只显示这一条，与「我当前只有一个配置」的事实一致。

    顺序也不能换：**先读激活指针 → 再删（排除它）→ 最后校验指针是否还指得着**。

    对旧数据意味着什么
    ------------------
    - 升级前没配过供应商的库：六条空壳全删，列表为空，运行时走 env 合成（行为不变）。
    - 升级前只配过 ark 并激活：只留 ark 一条，其余五条消失。
    - 改过任意一条内置配置：那条一行不删，只是失去 `builtin` 标记。
    删除不可逆，发布前请做一次库文件备份。
    """
    # ① 快照列：新库由 `SCHEMA_SQL` 建好，这里只补旧库（PRAGMA 是事实判断，不靠版本号猜）
    columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_jobs)")}
    for name, ddl in _JOB_PROVIDER_SNAPSHOT_COLUMNS:
        if name not in columns:
            conn.execute(f"ALTER TABLE generation_jobs ADD COLUMN {name} {ddl}")

    # ② 历史回填。坏 JSON 会让 `json_extract` 直接抛错（整个库起不来），
    #    所以 `json_valid` / `json_type` 两个守卫必须写在 WHERE 里，不能指望 COALESCE。
    #    末尾的 `provider_protocol = ''` 让这条语句重跑时幂等（不会覆盖已写入的值）。
    #    历史行只有 `provider`（协议名）与 `model`，没有 config_id / name，留空即可。
    conn.execute(
        """
        UPDATE generation_jobs
           SET provider_protocol  = COALESCE(CAST(json_extract(usage, '$.provider') AS TEXT), ''),
               provider_model     = COALESCE(CAST(json_extract(usage, '$.model') AS TEXT), ''),
               provider_name      = COALESCE(CAST(json_extract(usage, '$.provider_name') AS TEXT), ''),
               provider_config_id = COALESCE(CAST(json_extract(usage, '$.provider_config_id') AS TEXT), '')
         WHERE usage IS NOT NULL
           AND json_valid(usage)
           AND json_type(usage) = 'object'
           AND provider_protocol = ''
        """
    )

    # ③ 先读激活指针：它可能是 JSON 文本（`"ark"`），也可能被手工改坏
    active_id: str | None = None
    row = conn.execute(
        "SELECT value FROM system_config WHERE key = 'active_provider_id'"
    ).fetchone()
    if row is not None:
        try:
            decoded = json.loads(row[0])
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, str) and decoded:
            active_id = decoded

    # ④ 删出厂空壳（四重与）
    placeholders = ", ".join("?" for _ in _V16_SEED_PROVIDER_IDS)
    doomed = [
        str(found[0])
        for found in conn.execute(
            f"SELECT id FROM model_providers "
            f"WHERE builtin = 1 AND created_at = updated_at "
            f"  AND id IN ({placeholders}) AND id IS NOT ?",
            (*_V16_SEED_PROVIDER_IDS, active_id),
        )
    ]
    if doomed:
        conn.executemany(
            "DELETE FROM model_providers WHERE id = ?", [(pid,) for pid in doomed]
        )
        logger.info(
            "v19 清掉出厂供应商空壳 %d 条：%s",
            len(doomed),
            "、".join(doomed),
            extra={"event": "migration.provider_seed_removed", "count": len(doomed)},
        )

    # ⑤ 留下的内置行降级为普通条目（可编辑、可删除）
    conn.execute("UPDATE model_providers SET builtin = 0 WHERE builtin = 1")

    # ⑥ 指针兜底：删完之后若仍指向不存在的行（手工改库等），清掉该键。
    #    不清的后果是每次读配置都刷一条 WARN，且后台「当前供应商」与列表对不上。
    if active_id is not None:
        alive = {str(found[0]) for found in conn.execute("SELECT id FROM model_providers")}
        if active_id not in alive:
            conn.execute("DELETE FROM system_config WHERE key = 'active_provider_id'")
            logger.info(
                "v19 清掉指向不存在配置的激活指针：%s",
                active_id,
                extra={"event": "migration.active_pointer_cleared", "provider_id": active_id},
            )


def _model_providers_value_token(conn: sqlite3.Connection) -> None:
    """v20：密钥从「环境变量名指针」改为「主密钥加密后的密文」。

    背景：ADR-053 推翻了 ADR-042 的「密钥不进库」。此后每条配置的密钥用主密钥
    加密后直接存本表，env 不再放密钥。

    对旧数据的处理
    --------------
    `api_key_env_var` 里存的是**变量名**（如 `ARK_API_KEY`），不是密钥值，
    无法自动变成密文——密钥值本来就在 `.env` 里，且新方案要求它进库。
    所以这里**直接丢掉旧列**，管理员在后台「模型配置」页重新录入一次密钥。
    这是一次性的：丢掉变量名不影响任何历史数据（旧的密钥值仍在 `.env` 里，
    需要的话可以抄过去，然后删掉 `.env` 里的密钥行）。

    幂等
    ----
    新库由基线（`init_schema`）直接建成 `value_token`，本步骤是空操作；
    旧库先补 `value_token` 再删旧列。两处都用 `PRAGMA table_info` 守卫，重跑安全。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(model_providers)")}
    if not columns:
        return
    if "value_token" not in columns:
        conn.execute(
            "ALTER TABLE model_providers ADD COLUMN value_token TEXT NOT NULL DEFAULT ''"
        )
    if "api_key_env_var" in columns:
        # 旧列只是变量名，不含密钥值，删掉不丢任何可用数据。
        conn.execute("ALTER TABLE model_providers DROP COLUMN api_key_env_var")


# 迁移清单。顺序即执行顺序。新增结构一律追加到末尾，**不要修改历史步骤**。
# 每一版的内容请在该步骤的 docstring 里写清「为什么需要它、对旧数据意味着什么」。
# ⚠️ v19 起出现「数据清理型」迁移：只允许删**可由结构重建的出厂/派生数据**，
#    条件必须写成多重与（`builtin` + 未编辑 + id 白名单 + 非激活项），且 docstring
#    要写明「删了之后怎么恢复」——删除是单向门，`user_version` 只会前进。
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
    Migration(version=13, name="app_logs_table", apply=_create_app_logs_table),
    Migration(version=14, name="jobs_token_columns", apply=_add_token_columns_to_jobs),
    Migration(version=15, name="admin_audit_log_table", apply=_create_admin_audit_log_table),
    Migration(version=16, name="runtime_config_tables", apply=_create_runtime_config_tables),
    Migration(version=17, name="users_quota_columns", apply=_add_quota_columns_to_users),
    Migration(version=18, name="login_failures_table", apply=_create_login_failures_table),
    Migration(
        version=19,
        name="provider_config_entries",
        apply=_provider_config_entries,
    ),
    Migration(
        version=20,
        name="model_providers_value_token",
        apply=_model_providers_value_token,
    ),
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
