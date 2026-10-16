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


# 迁移清单。顺序即执行顺序。新增结构一律追加到末尾，**不要修改历史步骤**。
# 每一版的内容请在该步骤的 docstring 里写清「为什么需要它、对旧数据意味着什么」。
MIGRATIONS: list[Migration] = [
    Migration(version=1, name="documents_table", apply=_create_documents_table),
    Migration(version=2, name="backfill_documents", apply=_backfill_documents),
    Migration(version=3, name="jobs_pending_items", apply=_add_pending_items_to_jobs),
    Migration(version=4, name="document_versions_table", apply=_create_document_versions_table),
    Migration(version=5, name="backfill_document_versions", apply=_backfill_document_versions),
    Migration(version=6, name="documents_drop_content_columns", apply=_drop_documents_content_columns),
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
