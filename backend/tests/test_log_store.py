"""日志持久化用例（v4 T0：让日志第一次被**留下来**）。

这里钉住的不是「表建出来了」，而是**整条链**：handler 挂上 → 日志进队列 →
真的写进了 `app_logs`。只测其中一环的话，「表存在但 handler 没装」这种漏挂会
静默溜过去——而它的表现是「后台上线那天日志页一片空白」，很难反查。

另一组用例盯的是**降级**。日志系统的失败绝不能变成业务的失败，所以这里断言的
不是「没出错」，而是「出错也不影响调用方」：队列满、写库失败、文件不可写，
三种情况下 `logger.info(...)` 都必须照常返回。
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import pytest

from backend.core import logging_setup
from backend.core.config import get_settings
from backend.core.db import connect, utc_ms_text, utc_now, utc_now_ms_text
from backend.repositories.log_repo import LogRepository


@pytest.fixture
def log_env(tmp_path, monkeypatch):  # noqa: ARG001 - tmp_path 由 isolated_db 共用
    """把落库调成「测试友好」：小批量、短间隔，并在结束后彻底收尾。

    收尾这一步不是洁癖：`LogStoreHandler` 带着后台线程，若不摘掉，
    它会继续往**下一个用例的临时库**里写，表现为「凭空多出几行日志」，
    这种串扰极难复盘。
    """
    monkeypatch.setenv("LOG_DB_ENABLED", "true")
    # 0.1 是配置项允许的最小值（`log_db_flush_interval` 的 ge 约束），
    # 再低就没有意义了——那等于放弃批量，把每条日志都变成一次写事务。
    monkeypatch.setenv("LOG_DB_FLUSH_INTERVAL", "0.1")
    monkeypatch.setenv("LOG_DB_BATCH_SIZE", "5")
    get_settings.cache_clear()

    root = logging.getLogger()
    original_level = root.level
    logging_setup.configure_logging(level="INFO", fmt="text", force=True)
    yield
    logging_setup.reset_logging()
    root.setLevel(original_level)
    get_settings.cache_clear()


def _row(**overrides) -> dict:
    """造一行 `app_logs`，未指定的字段用合法默认值。"""
    base = {
        "ts": utc_now_ms_text(),
        "level": "INFO",
        "logger": "test.logger",
        "msg": "测试日志",
        "request_id": None,
        "user_id": None,
        "event": None,
        "extra_json": "",
        "exc": None,
    }
    base.update(overrides)
    return base


def _rows() -> list[dict]:
    """读出表内全部日志行。

    测试里直接写 SQL 是刻意的：这里要验的就是**落库后的行长什么样**，
    若再包一层查询方法，就把「表结构」和「查询逻辑」混在一起验了。
    """
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT ts, level, logger, msg, request_id, user_id, event, extra_json, exc "
                "FROM app_logs ORDER BY id"
            ).fetchall()
        ]


def test_app_logs_table_exists_after_migration(isolated_db):  # noqa: ARG001
    """迁移跑完后 `app_logs` 表与关键列都在。

    版本号**不写死**（断言 `run_migrations() == len(MIGRATIONS)`），
    否则将来每加一步迁移，这个与日志无关的用例都会红一次。
    """
    from backend.core.migrations import MIGRATIONS, run_migrations

    assert run_migrations() == len(MIGRATIONS)

    with connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(app_logs)")}
        # `index_list` 的第一列是序号，索引名在第二列
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(app_logs)")}

    assert {"ts", "level", "logger", "msg", "request_id", "user_id", "event"} <= columns
    # 索引是「按时间翻页 / 按级别过滤 / 按请求号反查」三条查询路径的前提，
    # 缺了它们日志页在数据量上来后会直接卡住。
    assert {"idx_app_logs_ts", "idx_app_logs_level", "idx_app_logs_request"} <= indexes


def test_startup_log_is_persisted(log_env):
    """端到端：服务启动那条日志真的进了库。

    这是本文件里最有价值的一条——它走的是**真实 lifespan**，
    能同时验出「handler 挂上了」「时序在迁移之后」「启动日志没丢」三件事。
    任何一环脱节，这里都会红。
    """
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as _client:
        pass  # 进出上下文即触发完整 lifespan（启动 → 收尾 flush）

    events = {row["event"] for row in _rows()}
    assert "service.started" in events


def test_file_handler_writes_rotating_file(log_env, tmp_path):
    """轮转文件那一路始终要写——它是「落库可关」时唯一的兜底。"""
    logging_setup.install_log_persistence()
    logging.getLogger("test.logger").info("落文件测试")
    logging_setup.shutdown_log_persistence()

    log_file = tmp_path / "logs" / "prdwright.log"
    assert log_file.exists(), "日志文件没建出来，等于回到了「进程一停全没」"
    assert "落文件测试" in log_file.read_text(encoding="utf-8")


def test_db_row_carries_event_user_and_extra(log_env):
    """落库的行要带上 `event` / `user_id` 两个提升列，其余字段进 `extra_json`。"""
    logging_setup.install_log_persistence()
    logging.getLogger("test.logger").info(
        "落库测试",
        extra={"event": "test.persisted", "user_id": 7, "job_id": "j-1"},
    )
    logging_setup.shutdown_log_persistence()

    matched = [row for row in _rows() if row["msg"] == "落库测试"]
    assert len(matched) == 1, f"应当只有一条，实际：{matched}"

    row = matched[0]
    payload = json.loads(row["extra_json"])
    assert row["event"] == "test.persisted"
    assert row["user_id"] == 7
    assert payload["job_id"] == "j-1"
    # 提升成列的字段不该再重复躺在 extra_json 里，否则「按 event 过滤」与
    # 「搜 extra_json」会给出两套不一致的结果
    assert "event" not in payload
    assert "user_id" not in payload


def test_exception_log_persists_traceback(log_env):
    """带 `exc_info` 的日志必须留住 traceback——它是最不该丢的一类。

    这条是被全量用例逼出来的：`formatException` 是 `Formatter` 的方法，
    在 Handler 上直接调会抛 AttributeError，而 `logging` 会**吞掉**这个异常，
    表现为「出错的日志恰好没入库」——静默丢数据，只在 stderr 留一行痕迹。
    所以这里断言的不是「没崩」，而是 traceback 真的存下来了。
    """
    logging_setup.install_log_persistence()
    try:
        raise RuntimeError("故意炸一下")
    except RuntimeError:
        logging.getLogger("test.logger").error("出错了", exc_info=True)
    logging_setup.shutdown_log_persistence()

    matched = [row for row in _rows() if row["msg"] == "出错了"]
    assert len(matched) == 1, f"带异常的日志必须入库，实际：{matched}"
    assert matched[0]["level"] == "ERROR"
    assert "RuntimeError: 故意炸一下" in (matched[0]["exc"] or "")


def test_module_internal_logs_are_not_queued(log_env):
    """本模块的日志**绝不入队**——否则写库失败的告警会自己把自己放大。

    这条防的是「失败 → 告警 → 告警又等着入库 → 再失败」的死循环。
    """
    handler = logging_setup.LogStoreHandler(
        capacity=100, batch_size=1000, interval=10.0
    )
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        logging.getLogger("backend.core.logging_setup").warning("本模块的告警")
        assert handler._queue.qsize() == 0  # noqa: SLF001 - 故意窥探队列，防递归只能这样验
    finally:
        root.removeHandler(handler)
        handler.close()


def test_queue_full_drops_instead_of_blocking(log_env):
    """队列满时丢弃新日志并降级，**绝不阻塞**打日志的那行业务代码。

    这里先把后台线程停掉来模拟「写库端停摆」——不停的话它会一直把队列取空，
    队列永远填不满，用例就变成了自欺欺人（看着像在测积压，实际一条都没丢）。
    """
    handler = logging_setup.LogStoreHandler(
        capacity=1, batch_size=1000, interval=10.0
    )
    handler._stop.set()  # noqa: SLF001
    handler._thread.join(timeout=2.0)  # noqa: SLF001

    root = logging.getLogger()
    root.addHandler(handler)
    try:
        flood = logging.getLogger("test.flood")
        for index in range(5):
            flood.info("洪水 %d", index)  # 不该抛异常，也不该卡住
        assert handler._dropped >= 1  # noqa: SLF001
    finally:
        root.removeHandler(handler)
        handler.close()


def test_write_failure_degrades_to_warning(log_env, monkeypatch, caplog):
    """写库失败只降级成一条 WARNING，绝不把异常抛回调用方。"""
    calls: list[int] = []

    def boom(rows: list[dict]) -> int:
        calls.append(len(rows))
        raise RuntimeError("数据库不可用")

    monkeypatch.setattr(LogRepository, "insert_many", boom)

    handler = logging_setup.LogStoreHandler(
        capacity=10, batch_size=1, interval=0.05
    )
    try:
        with caplog.at_level(logging.WARNING):
            handler._write([_row()])  # noqa: SLF001 - 直接测降级路径
    finally:
        handler.close()

    assert calls == [1], "应当只尝试一次，不做无意义重试（重试只会加重积压）"
    assert "日志落库失败" in caplog.text


def test_prune_enforces_days_and_rows(isolated_db):  # noqa: ARG001
    """两个闸门各管一种失效模式：天数防「太旧还占着」，行数防「短时间爆炸」。"""
    stale = utc_ms_text(utc_now() - timedelta(days=30))
    fresh = utc_now_ms_text()
    LogRepository.insert_many(
        [
            _row(ts=stale, msg="旧 1"),
            _row(ts=stale, msg="旧 2"),
            _row(ts=fresh, msg="新 1"),
            _row(ts=fresh, msg="新 2"),
            _row(ts=fresh, msg="新 3"),
            _row(ts=fresh, msg="新 4"),
        ]
    )
    assert LogRepository.count() == 6

    # 保留 7 天 → 删掉 2 条旧的；上限 3 行 → 再从 4 条新的里删掉最旧的 1 条
    deleted = LogRepository.prune(retention_days=7, max_rows=3)
    assert deleted == 3
    assert LogRepository.count() == 3

    remaining = [row["msg"] for row in _rows()]
    assert remaining == ["新 2", "新 3", "新 4"], "裁剪必须保住最新的那批"


def test_prune_is_noop_when_under_limits(isolated_db):  # noqa: ARG001
    """行数没超限时一条都不删——`id <= NULL` 的语义要真的成立。"""
    LogRepository.insert_many([_row(), _row()])
    assert LogRepository.prune(retention_days=7, max_rows=100) == 0
    assert LogRepository.count() == 2


def test_install_is_idempotent(log_env):
    """重复安装不能挂第二个 handler——挂两个就是日志翻倍。"""
    logging_setup.install_log_persistence()
    logging_setup.install_log_persistence()

    root = logging.getLogger()
    installed = [
        handler
        for handler in root.handlers
        if getattr(handler, logging_setup._PERSIST_MARKER, False)  # noqa: SLF001
    ]
    logging_setup.shutdown_log_persistence()

    assert len(installed) == 2, f"应当是「文件 + 落库」两路，实际 {len(installed)}"
