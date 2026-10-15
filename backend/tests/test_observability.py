"""可观测性基线（R2-T7）的契约测试。

守两条底线：

1. **日志带得上 `request_id`**——用户手上那个号必须能在日志里搜到。否则就是
   「响应头上有号、日志里查不到」，追踪链断在最后一步，而这个号也就白给了。
2. **`/health` 说得出是哪一类不可用**——数据库、供应商、技能包三项分开报，
   而不是一句「我活着」（活着但没法用才是常态）。

另外守住一个容易被悄悄破坏的性质：**`LOG_FORMAT` 配置要真的接上**。
配置项写了却没人读，是这类"加了功能但没生效"的最常见形态。
"""

from __future__ import annotations

import json
import logging
import sys

import pytest

from backend.core.config import get_settings
from backend.core.logging_setup import (  # noqa: PLC2701 - 断言要按标记认 handler，不能靠位置猜
    _HANDLER_MARKER,
    JsonFormatter,
    TextFormatter,
    configure_logging,
    reset_logging,
)
from backend.core.request_context import reset_request_id, set_request_id

from conftest import PRD_SKILL_ARTIFACTS, write_skill_package


@pytest.fixture(autouse=True)
def _clean_logging():
    """每个用例前后都把日志配置清干净。

    `configure_logging` 是装在 root logger 上的**进程级副作用**。不清的话，
    前一个用例配置过的那份会让后一个用例里「配置确实生效了」的断言永远通过——
    那时测的是测试顺序，不是代码。
    """
    reset_logging()
    yield
    reset_logging()


@pytest.fixture
def make_client(isolated_db, monkeypatch):  # noqa: ARG001
    """按需构造带生命周期的测试客户端，并可先注入环境变量。

    `get_settings` 是带缓存的单例（改 env 必须 `cache_clear()`，否则读到上一个用例的
    配置），而配置必须在 **`TestClient` 进入生命周期之前**就位——否则验的是旧配置。
    把这两步封在一起，用例里只需写清楚「我要哪些配置」。
    """
    created = []

    def _make(**env: str):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from fastapi.testclient import TestClient

        from backend.main import app

        client = TestClient(app)
        client.__enter__()
        created.append(client)
        return client

    yield _make

    for client in created:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 健康检查：三项分开报
# ---------------------------------------------------------------------------


def test_health_reports_three_checks(make_client):
    """三项齐全且都能过时，status 才是 `ok`。"""
    client = make_client(
        DEFAULT_LLM_PROVIDER="ark",
        ARK_API_KEY="test-key-not-real",
    )

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"database", "llm", "skills"}
    for name, item in body["checks"].items():
        assert item["ok"] is True, f"{name} 本应通过：{item}"


def test_health_reports_llm_without_pretending_to_call_it(make_client):
    """供应商这一项只报「配置就位」，并且**明说不发真实调用**。

    健康检查会被探针高频触发，让它按次烧额度不合适。既然没真的调过，
    就不能让文案读起来像「已验证可用」——不诚实的绿灯比红灯更贵。
    """
    client = make_client(
        DEFAULT_LLM_PROVIDER="ark",
        ARK_API_KEY="test-key-not-real",
        DEFAULT_LLM_MODEL="doubao-seed-2-1-turbo-260628",
    )

    llm = client.get("/health").json()["checks"]["llm"]

    assert llm["ok"] is True
    assert llm["provider"] == "ark"
    assert llm["model"] == "doubao-seed-2-1-turbo-260628"
    assert "未发起真实调用" in llm["detail"]


def test_health_flags_missing_api_key(make_client):
    """密钥没配是「生成必然失败」的头号原因，必须在体检里报出来。"""
    client = make_client(DEFAULT_LLM_PROVIDER="ark", ARK_API_KEY="")

    body = client.get("/health").json()

    assert body["status"] == "degraded"
    assert body["checks"]["llm"]["ok"] is False
    assert "ARK_API_KEY" in body["checks"]["llm"]["detail"]
    # 只这一项不过，其余两项仍应通过——分开报的意义就在这里
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["skills"]["ok"] is True


def test_health_flags_absent_skills_directory(make_client, tmp_path):
    """skills 根目录整个不在位（部署漏打包）时要报出来，而不是让生成静默退回内置提示词。"""
    client = make_client(
        DEFAULT_LLM_PROVIDER="ark",
        ARK_API_KEY="test-key-not-real",
        # 指到一个不存在的目录：扫描结果为空，一个技能包也找不到
        PRDWRIGHT_SKILLS_DIR=str(tmp_path / "no-skills-here"),
    )

    body = client.get("/health").json()

    assert body["status"] == "degraded"
    skills = body["checks"]["skills"]
    assert skills["ok"] is False
    assert "未发现任何技能包" in skills["detail"]


def test_health_reports_missing_skill_asset_by_name(make_client, tmp_path):
    """资源缺了要报出缺的是哪个文件——照着自己补就行。"""
    skills_root = tmp_path / "skills"
    skill_dir = write_skill_package(skills_root, "prd-generator")
    (skill_dir / PRD_SKILL_ARTIFACTS["writing_rules"]).unlink()

    client = make_client(
        DEFAULT_LLM_PROVIDER="ark",
        ARK_API_KEY="test-key-not-real",
        PRDWRIGHT_SKILLS_DIR=str(skills_root),
    )

    skills = client.get("/health").json()["checks"]["skills"]

    assert skills["ok"] is False
    assert "缺少技能资源" in skills["detail"]
    assert "writing-rules.md" in skills["detail"]


def test_health_reports_skill_identity(make_client, tmp_path):
    """齐备时报出加载了哪些包、各自服务哪些产物，并带上与回执同源的指纹。

    与注册表相关的这一项不再是「只认 PRD 那一个包」：新技能包自己声明 `applies_to`
    就会出现在这里，体检不必跟着改。
    """
    skills_root = tmp_path / "skills"
    write_skill_package(skills_root, "prd-generator", version="0.4.0")

    client = make_client(
        DEFAULT_LLM_PROVIDER="ark",
        ARK_API_KEY="test-key-not-real",
        PRDWRIGHT_SKILLS_DIR=str(skills_root),
    )

    skills = client.get("/health").json()["checks"]["skills"]

    assert skills["ok"] is True
    assert len(skills["skills"]) == 1
    entry = skills["skills"][0]
    assert entry["name"] == "prd-generator"
    assert entry["version"] == "0.4.0"
    assert entry["applies_to"] == ["prd"]
    # 指纹是 12 位十六进制，与回执上那个能直接对上
    assert len(entry["fingerprint"]) == 12


def test_health_flags_unavailable_database(isolated_db, monkeypatch, tmp_path):  # noqa: ARG001
    """数据库连不上要报出来。

    这里直接调探测函数而不是走 HTTP：把 `DB_PATH` 指到非法路径后，
    连应用启动（`init_schema`）都会失败，根本到不了能发请求的状态——
    而「体检自己要能在这种时候给出结论」正是它的用途。
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("占位文件，让同名的目录建不出来", encoding="utf-8")
    monkeypatch.setenv("DB_PATH", str(blocker / "test.db"))
    get_settings.cache_clear()

    from backend.api.health import _check_database

    result = _check_database()

    assert result["ok"] is False
    # 只给异常类型：`/health` 是公开路径，不往外吐路径等内部细节
    assert "不可用" in result["detail"]


# ---------------------------------------------------------------------------
# 访问日志：每条都带得上号
# ---------------------------------------------------------------------------


def test_request_log_carries_id_matching_response_header(client, caplog):
    """日志里的号必须与响应头里的号是同一个——否则用户报障时念的号在日志里搜不到。"""
    with caplog.at_level(logging.INFO, logger="backend.api.access"):
        response = client.get("/health", headers={"X-Request-ID": "log-trace-1"})

    records = [item for item in caplog.records if item.name == "backend.api.access"]
    assert records, "访问日志一条都没记"

    record = records[-1]
    assert record.request_id == "log-trace-1"
    assert record.path == "/health"
    assert record.method == "GET"
    assert record.status == 200
    assert record.duration_ms >= 0
    assert response.headers["X-Request-ID"] == record.request_id


def test_unauthorized_request_is_also_logged(client, caplog):
    """被认证拦下的 401 也要有日志。

    「用户说打不开」时第一个要确认的问题就是「这个请求到底有没有到过后端」，
    而认证拦下的请求恰恰是最容易一条日志都不留下的那类。
    """
    with caplog.at_level(logging.INFO, logger="backend.api.access"):
        response = client.get("/api/jobs/999")

    assert response.status_code == 401
    records = [item for item in caplog.records if item.name == "backend.api.access"]
    assert records
    assert records[-1].status == 401
    assert records[-1].request_id == response.headers["X-Request-ID"]


# ---------------------------------------------------------------------------
# 格式化器：两种格式带同一组字段
# ---------------------------------------------------------------------------


def _make_record(**extra) -> logging.LogRecord:
    """造一条带业务字段的日志记录，用于直接验格式化器。"""
    record = logging.LogRecord(
        name="backend.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=12,
        msg="生成第 %s 章",
        args=("3",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_one_line_with_id_and_extras():
    """JSON 模式：一行一条、字段带全，`extra=` 的业务字段自动带出。"""
    token = set_request_id("req-abc")
    try:
        line = JsonFormatter().format(_make_record(job_id=7, step="prd:writing"))
    finally:
        reset_request_id(token)

    assert "\n" not in line, "一条日志必须只有一行，否则采集端按行切会切坏"
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "backend.test"
    assert payload["msg"] == "生成第 3 章"
    assert payload["request_id"] == "req-abc"
    assert payload["job_id"] == 7
    assert payload["step"] == "prd:writing"


def test_json_formatter_omits_absent_request_id():
    """没有请求上下文时不写这个键。

    `request_id: null` 与「这条日志本来就不属于任何请求」（例如启动日志）
    是两回事，混在一起会让按号过滤的查询多出噪声。
    """
    token = set_request_id("")
    try:
        payload = json.loads(JsonFormatter().format(_make_record()))
    finally:
        reset_request_id(token)

    assert "request_id" not in payload


def test_text_formatter_keeps_id_greppable():
    """文本模式也要能一眼搜到号——本地排查最常见的动作就是把回执上那个号粘进来搜。"""
    token = set_request_id("req-abc")
    try:
        line = TextFormatter().format(_make_record(job_id=7, status=200))
    finally:
        reset_request_id(token)

    assert "[req=req-abc]" in line
    assert "生成第 3 章" in line
    assert "job_id=7" in line
    assert "status=200" in line


def test_explicit_request_id_wins_over_context():
    """`extra=` 里显式带的号优先于上下文变量。

    访问日志是在 `reset_request_id` 之后才写出去的，那时上下文已经取不到值——
    请求号能留在日志里，靠的正是这条路。
    """
    token = set_request_id("from-context")
    try:
        record = _make_record(request_id="from-extra")
        assert json.loads(JsonFormatter().format(record))["request_id"] == "from-extra"
        assert "[req=from-extra]" in TextFormatter().format(record)
    finally:
        reset_request_id(token)


def test_request_id_is_not_rendered_twice_in_text():
    """文本行里号只出现一次，别既当标签又当业务字段。"""
    token = set_request_id("req-once")
    try:
        line = TextFormatter().format(_make_record(request_id="req-once"))
    finally:
        reset_request_id(token)

    assert line.count("req-once") == 1


def test_exception_is_rendered_in_both_formats():
    """异常信息两种格式都要带出去——把 `exc_info` 丢了等于日志只说了「出错了」三个字。"""
    try:
        raise ValueError("样例异常")
    except ValueError:
        exc_info = sys.exc_info()

    record = _make_record()
    record.exc_info = exc_info

    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: 样例异常" in payload["exc"]
    assert "ValueError: 样例异常" in TextFormatter().format(record)


# ---------------------------------------------------------------------------
# 配置接线：写了配置要真的有人读
# ---------------------------------------------------------------------------


def test_configure_logging_is_idempotent():
    """重复配置不能叠出第二个 handler——否则每条日志打两遍。"""
    configure_logging(level="INFO", fmt="text")
    first = _own_handlers()
    assert len(first) == 1

    # 再次调用（模拟 lifespan 被跑第二次、或测试里重复启动应用）：不叠加、也不改配置
    configure_logging(level="DEBUG", fmt="json")
    second = _own_handlers()
    assert len(second) == 1
    assert isinstance(second[0].formatter, TextFormatter)

    configure_logging(level="DEBUG", fmt="json", force=True)
    forced = _own_handlers()
    assert len(forced) == 1
    assert isinstance(forced[0].formatter, JsonFormatter)


def test_log_format_setting_reaches_root_logger(make_client):
    """`LOG_FORMAT=json` 必须真的把格式化器换掉（配置项写了没人读是最常见的假功能）。"""
    make_client(LOG_FORMAT="json", LOG_LEVEL="INFO")

    handlers = _own_handlers()
    assert len(handlers) == 1
    assert isinstance(handlers[0].formatter, JsonFormatter)


def test_unknown_log_level_does_not_break_startup(make_client):
    """级别名拼错退回 INFO，不让一个笔误把服务拦在启动之外。"""
    make_client(LOG_LEVEL="NOT-A-LEVEL")

    assert logging.getLogger().level == logging.INFO


def _own_handlers() -> list[logging.Handler]:
    """取本模块装在 root 上的 handler（按标记认，不靠位置猜）。"""
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, _HANDLER_MARKER, False)
    ]
