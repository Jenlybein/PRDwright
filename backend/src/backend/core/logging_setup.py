"""结构化日志：一行一条，且每条都带 `request_id`。

为什么要它
----------
`request_id` 在 R1 就贯通了——中间件生成它、`ContextVar` 传递它、回执上回给用户。
但**日志里没有它**：后端一直在用 `logging` 的默认兜底输出（裸文本，没有时间也没有
来源），出问题时只能按时间顺序猜「这几行属于哪一次生成」。同一个号出现在响应头上、
却在日志里查不到，追踪链就断在最后一步。

这里补上两件事：

1. **每条日志都带 `request_id`**：由格式化器从上下文变量现取，业务代码那一行
   （`logger.info("...")`）**一行都不用改**。加字段这件事只发生在一个地方，
   将来再加字段（如 `user_id`）也只改这里。
2. **可以是 JSON 行**：`LOG_FORMAT=json` 时输出一行一条 JSON，可直接喂给日志采集；
   默认 `text` 是给人看的（本地开发读 JSON 太痛苦）。两种格式带**同一组字段**，
   所以「本地能看懂」与「线上能检索」不冲突。

调用方通过 `extra=` 挂上的业务字段（`job_id` / `step` / `status` …）会被自动带出去，
不需要在格式化器里逐个列举——否则每加一个字段就得回来改一次格式化器，很快就会漏。

**为什么不用 `dictConfig`**：本项目只在 `lifespan` 里配置一次，还需要给 uvicorn
已经装好的 handler 换格式；声明式的配置字典在这个场景里反而更绕。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from backend.core.request_context import current_request_id

# `LogRecord` 自带的标准属性，外加本模块**单独处理**的 `request_id`。
# 凡是不在这个集合里的键，都视为调用方通过 `extra=` 挂上的业务字段。**这是个排除法**：
# 新增业务字段时不需要来这里登记（这正是用排除法而不是白名单的原因——漏登记就会静默丢字段）。
#
# `request_id` 之所以要列进来：两种格式都把它放在固定位置（JSON 一个键、文本一个
# `[req=]` 标签），若再当成普通业务字段走一遍，文本行里就会出现两次（`[req=abc] …
# request_id=abc`）。
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "request_id",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

# 挂在本模块装上的 handler 上的标记，用于「重配时先摘掉旧的」而不靠位置猜
_HANDLER_MARKER = "_prdwright_log_handler"

# 装在 root logger 上的标记，避免重复配置时叠出第二个 handler（日志翻倍）
_CONFIGURED_MARKER = "_prdwright_logging_configured"

# uvicorn 自己那几个 logger。它们的 handler 是 uvicorn 启动时装的，
# 我们只换格式、不另加 handler，否则同一行会出两遍。
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error")


def _utc_iso(timestamp: float) -> str:
    """把 `LogRecord.created`（epoch 秒）转成 UTC 的 ISO 8601，形如 `...T08:30:00.123Z`。"""
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    """挑出调用方用 `extra=` 挂上的业务字段。"""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRS and not key.startswith("_")
    }


def _request_id_of(record: logging.LogRecord) -> str:
    """这条日志该带哪个请求号。

    **显式传的那个优先**：访问日志是在 `reset_request_id` 之后才写出去的
    （见 `api/deps.py` 的 `finally`），那时上下文变量已经取不到值了，只有
    `extra={"request_id": ...}` 这条路还通。
    """
    explicit = getattr(record, "request_id", "")
    return explicit if explicit else current_request_id()


class JsonFormatter(logging.Formatter):
    """一行一条 JSON。

    字段固定为 `ts` / `level` / `logger` / `msg`，`request_id` 与业务字段按需附加
    ——**没有就不出现这个键**，而不是给个空串：查询时 `request_id:null` 与
    「这条日志本来就不属于任何请求」（如启动日志）是两回事。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_iso(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = _request_id_of(record)
        if request_id:
            payload["request_id"] = request_id
        # `extra=` 里的业务字段最后合并（`request_id` 已在上面单独处理，不再重复）
        payload.update(_extra_fields(record))
        if record.exc_info:
            # 多行 traceback 会被 json.dumps 转义成 `\n` 字面量，仍是完整的一行
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


class TextFormatter(logging.Formatter):
    """人读格式：`时间 级别 来源 [req=xxx] 消息 业务字段=值`。

    保留 `request_id` 的短标签形式，是因为本地排查最常见的动作就是「把回执上那个号
    粘进终端搜一下」——`req=` 这个前缀让它在满屏日志里一眼能定位。
    """

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            _utc_iso(record.created),
            f"{record.levelname:<5}",
            record.name,
        ]
        request_id = _request_id_of(record)
        if request_id:
            parts.append(f"[req={request_id}]")
        parts.append(record.getMessage())
        # 业务字段按 `key=value` 续在消息后面。顺序即 dict 的插入顺序，
        # 稳定可预期——用假名排序反而会让相关的字段被拆散。
        for key, value in _extra_fields(record).items():
            parts.append(f"{key}={value}")
        line = " ".join(parts)
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def _build_formatter(fmt: str) -> logging.Formatter:
    """按名字造格式化器；认不出的名字退回 `text`（配置写错不该让服务起不来）。"""
    return JsonFormatter() if fmt.lower() == "json" else TextFormatter()


def configure_logging(*, level: str = "INFO", fmt: str = "text", force: bool = False) -> None:
    """给 root logger 装上本项目的 handler，并把 uvicorn 的日志格式统一过来。

    幂等：已经配置过就直接返回（`force=True` 时先摘掉本模块上次装的 handler 再重配），
    避免重复调用叠出多个 handler 让每条日志打多遍。

    Args:
        level: 日志级别名，如 `INFO` / `DEBUG`；认不出的名字退回 `INFO`。
        fmt: `text`（给人看）或 `json`（结构化，一行一条）。
        force: 强制重配。测试里换格式时用得上。
    """
    root = logging.getLogger()
    if getattr(root, _CONFIGURED_MARKER, False) and not force:
        return

    formatter = _build_formatter(fmt)

    # 先摘掉上一次装的，保证「重配」不会变成「叠加」
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)

    root.setLevel(_resolve_level(level))
    setattr(root, _CONFIGURED_MARKER, True)

    # uvicorn 启动时已给这几个 logger 装了 handler（且 `propagate=False`，
    # 所以它们不会走到 root 的这个新 handler）。只换格式，不另加 —— 加了就是两行。
    for name in _UVICORN_LOGGERS:
        for existing in logging.getLogger(name).handlers:
            existing.setFormatter(formatter)

    # 访问日志由 `RequestIdMiddleware` 自己记（含 `request_id`/耗时/状态码），
    # uvicorn 那一条只是同样的信息、但没有 `request_id`——留两行等于每次请求
    # 都要多看一眼才能确认「哪行是有号的那条」。抬高级别而不是 `disabled = True`：
    # 后者会让「为什么我的访问日志没了」变成一个需要翻代码才能回答的问题，
    # 而级别是可以被 `--log-level` 之类的外部配置覆盖回去的。
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def reset_logging() -> None:
    """撤销 `configure_logging` 的效果，回到「没配置过」的状态。

    主要给测试用：用例之间必须互不影响，否则「前一个用例配置过」会让后一个用例里
    「配置确实生效了」的断言永远通过——那是在测测试顺序，不是在测代码。
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            root.removeHandler(existing)
    if hasattr(root, _CONFIGURED_MARKER):
        delattr(root, _CONFIGURED_MARKER)


def _resolve_level(name: str) -> int:
    """把级别名转成数值；认不出时退回 `INFO`。"""
    value = logging.getLevelName(name.strip().upper())
    return value if isinstance(value, int) else logging.INFO
