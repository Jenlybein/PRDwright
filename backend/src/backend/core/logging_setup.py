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

持久化（v4）
------------
上面解决的是「日志长什么样」，没解决「日志去了哪里」——在 v4 之前它只进 stdout，
进程一停就什么都不剩。所以这里多挂两路 handler（见 `install_log_persistence`）：

- **轮转文件**：永远开。它是「不丢」的兜底，也是唯一能脱机排查的东西。
- **`app_logs` 表**：可关。它让后台能过滤、分页、按请求号反查。

两路都是**加 handler 而已**，业务代码那一行 `logger.info(...)` 依旧不用改——
这与 `request_id` 的注入是同一条纪律：横切能力只在一处接线。
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

from backend.core.config import get_settings
from backend.core.db import utc_ms_text
from backend.core.request_context import current_request_id
from backend.repositories.log_repo import LogRepository

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

# 持久化 handler 的标记。**刻意与 `_HANDLER_MARKER` 分开**：两者生命周期不同——
# stdout handler 跟着 `configure_logging` 重配（测试换格式时要摘掉重装），
# 持久化 handler 跟着进程启停（它持有后台线程，不该被反复重建）。
_PERSIST_MARKER = "_prdwright_log_persist"

# 已挂上的持久化 handler。收在这里是为了进程退出时能统一收尾（flush + 关线程），
# 也为了防止重复安装——装两个就会双写，日志直接翻倍。
_persist_handlers: list[logging.Handler] = []

# 本模块自己的 logger。降级告警都用它，而 `LogStoreHandler` 会**跳过本模块的日志**，
# 于是「写库失败」这件事不会再去排队等写库（见 `LogStoreHandler.emit` 的说明）。
_module_logger = logging.getLogger(__name__)


def _utc_iso(timestamp: float) -> str:
    """把 `LogRecord.created`（epoch 秒）转成 UTC 的 ISO 8601，形如 `...T08:30:00.123Z`。

    刻意复用 `db.utc_ms_text` 而不是在这里再拼一遍格式：`app_logs.ts` 存的就是它，
    而日志裁剪要用字符串比较时间。**同一份实现**才能保证两者永不漂移。
    """
    return utc_ms_text(datetime.fromtimestamp(timestamp, tz=timezone.utc))


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


class LogStoreHandler(logging.Handler):
    """把日志**异步批量**写进 `app_logs` 表。

    为什么必须异步
    --------------
    `logger.info(...)` 常常发生在生成主链路的关键路径上。在这里同步写库，等于让
    「数据库卡一下」直接变成「生成卡一下」——日志把业务拖垮是最糟的失败模式，
    因为它连报错都难归因（看起来是生成慢，实际是写日志慢）。

    所以 `emit` 只做一件事：把记录放进内存队列，**非阻塞**。真正的写库交给后台线程
    批量完成。**队列满就丢弃新日志**并降级告警：丢日志可以接受，拖慢生成不行。

    失败时的行为是本类的设计核心：**任何异常都被吞掉**，只留一条 WARNING。
    日志系统的职责是「尽力留下线索」，不是「保证送达」，更不是「把错误抛给业务」。
    """

    def __init__(
        self,
        *,
        capacity: int,
        batch_size: int,
        interval: float,
        min_level: int = logging.INFO,
    ) -> None:
        super().__init__(level=min_level)
        # 只为格式化 traceback 而持有：`formatException` 是 `Formatter` 的方法，
        # `Handler` 本身没有。没有它，带 `exc_info` 的日志会在 `emit` 里抛
        # AttributeError——那类日志恰好是最该留住的（出错现场），
        # 却会静默丢掉、只在 stderr 留一行痕迹。
        self._formatter = logging.Formatter()
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=capacity)
        self._batch_size = batch_size
        self._interval = interval
        # 累计丢弃条数。它也是告警的限频计数器（见 `emit`）。
        self._dropped = 0
        self._stop = threading.Event()
        # daemon=True：即使漏调 `shutdown_log_persistence`，进程也能正常退出，
        # 不至于被一条日志线程挂住。代价是退出瞬间可能丢最后一批——可接受。
        self._thread = threading.Thread(target=self._drain, name="log-store", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        """把一条日志塞进队列；塞不进就丢掉并记录降级。"""
        # 本模块自己的日志**绝不入队**：写库失败时的告警正是从这里发出的，
        # 若它也去排队等入库，就会变成「失败 → 告警 → 再失败」的自我放大。
        # 它仍然会被 stdout 与文件 handler 记下——那正是降级信息该去的地方。
        if record.name == __name__:
            return
        try:
            self._queue.put_nowait(self._row(record))
        except queue.Full:
            self._dropped += 1
            # 告警**限频**（每 100 条喊一次）：队列持续满时，若每条都喊一声，
            # 「喊告警」本身就变成了新的日志风暴，把真正的问题埋掉。
            if self._dropped % 100 == 1:
                _module_logger.warning(
                    "日志落库积压，已丢弃 %d 条（队列容量 %d）。日志仍在写文件，业务不受影响",
                    self._dropped,
                    self._queue.maxsize,
                    extra={"event": "log.db_backlog_dropped", "dropped": self._dropped},
                )

    def _row(self, record: logging.LogRecord) -> dict[str, Any]:
        """把一条日志摊成 `app_logs` 的一行。

        两个字段被**提升成列**：`event`（分类检索的主维度）与 `user_id`（归属）。
        其余 `extra=` 字段原样进 `extra_json`——不为它们各开一列，
        是因为业务字段会不断新增，而表结构不该跟着每次新增都迁移一次。
        """
        extra = _extra_fields(record)
        event = extra.pop("event", None)
        raw_user = extra.pop("user_id", None)
        user_id: int | None = None
        if raw_user is not None:
            try:
                user_id = int(raw_user)
            except (TypeError, ValueError):
                # 带不出归属就留空，不要因为一个字段的格式问题丢掉整条日志
                user_id = None
        return {
            "ts": _utc_iso(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            # 空串存成 NULL：「不属于任何请求」与「请求号是空串」是两回事
            "request_id": _request_id_of(record) or None,
            "user_id": user_id,
            "event": event if isinstance(event, str) and event else None,
            "extra_json": (
                json.dumps(extra, ensure_ascii=False, separators=(",", ":"), default=str)
                if extra
                else ""
            ),
            "exc": (
                self._formatter.formatException(record.exc_info) if record.exc_info else None
            ),
        }

    def _drain(self) -> None:
        """后台线程：攒够一批或等超时就写库，直到收到停止信号。"""
        while not self._stop.is_set():
            batch = self._collect()
            if batch:
                self._write(batch)
        # 收尾：把停止信号发出前剩下的尽量写掉。少了这一步，「最后一次生成」的
        # 日志会凭空消失——而那恰恰是最需要看的几行。
        tail: list[dict[str, Any]] = []
        while len(tail) < self._batch_size * 10:
            try:
                tail.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if tail:
            self._write(tail)

    def _collect(self) -> list[dict[str, Any]]:
        """攒一批：够 `batch_size` 条就立刻返回，否则等到 `interval` 超时。

        ⚠️ 等待被切成 **≤50ms 的小段**而不是一次等到 `interval`：一次性等满的话，
        停止信号要等这整段走完才会被看见——`interval` 配成 10 秒时，关服务就得
        干等 10 秒，而那期间正是「最后一批日志还没落库」的窗口。切成小段后，
        停止信号最多延迟 50ms 生效，代价只是每 50ms 一次空转唤醒（可忽略）。
        """
        batch: list[dict[str, Any]] = []
        deadline = time.monotonic() + self._interval
        while len(batch) < self._batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._stop.is_set():
                break
            try:
                batch.append(self._queue.get(timeout=min(remaining, 0.05)))
            except queue.Empty:
                continue
        return batch

    def _write(self, batch: list[dict[str, Any]]) -> None:
        """写一批；失败只降级，绝不抛出（抛出会打断后台线程，此后日志全丢）。"""
        try:
            LogRepository.insert_many(batch)
        except Exception:  # noqa: BLE001 - 日志写入的任何失败都必须就地吞掉
            _module_logger.warning(
                "日志落库失败，%d 条未入库（仍已写入日志文件）",
                len(batch),
                exc_info=True,
                extra={"event": "log.db_write_failed", "count": len(batch)},
            )

    def close(self) -> None:
        """停线程并 flush 剩余日志。"""
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            # 超时不能太长：它是「关服务」路径上的一步，卡住就等于关不掉服务
            self._thread.join(timeout=5.0)
        super().close()


def _current_formatter(root: logging.Logger) -> logging.Formatter | None:
    """从已装的 handler 上取回格式化器，让持久化输出与 stdout **长得一样**。

    持久化 handler 复用同一个 formatter，是为了「文件里的一行」和「控制台的一行」
    字段完全一致——否则同一次排障里两边对不上号，比不记还难用。
    """
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARKER, False) and handler.formatter:
            return handler.formatter
    return None


def install_log_persistence() -> None:
    """把「日志留下来」的两路 handler 挂上：轮转文件（必开）+ 落库（可关）。

    ⚠️ 调用时机必须在 `init_schema()` 与 `run_migrations()` **之后**——
    否则 `app_logs` 表还不存在，服务启动那批日志会全部写失败并降级，
    而「启动日志恰好没有」是最容易被忽略、也最影响排障的一种丢数据。

    幂等：重复调用不会挂第二个（日志翻倍）。这在 `--reload` 与测试里都可能发生。
    """
    if _persist_handlers:
        return

    settings = get_settings()
    root = logging.getLogger()
    formatter = _current_formatter(root) or _build_formatter(settings.log_format)

    # --- 轮转文件：永远开 ----------------------------------------------------
    # 它是「不丢」的兜底。落库可以关，文件不行——关了就退回 v4 之前「进程一停全没」。
    try:
        log_dir = settings.log_dir_path
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "prdwright.log",
            maxBytes=settings.log_file_max_bytes,
            backupCount=settings.log_file_backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        setattr(file_handler, _PERSIST_MARKER, True)
        root.addHandler(file_handler)
        _persist_handlers.append(file_handler)
    except OSError as exc:
        # 目录不可写时**不能让服务起不来**：日志是辅助设施，
        # 它挂了应该降级成「只输出控制台」，而不是把整个后端拦在启动之外。
        _module_logger.warning(
            "日志文件不可用（%s），日志将只输出到控制台",
            exc,
            extra={"event": "log.file_unavailable"},
        )

    # --- 落库：可关 ----------------------------------------------------------
    if not settings.log_db_enabled:
        return
    store = LogStoreHandler(
        capacity=settings.log_db_queue_size,
        batch_size=settings.log_db_batch_size,
        interval=settings.log_db_flush_interval,
        min_level=_resolve_level(settings.log_db_min_level),
    )
    setattr(store, _PERSIST_MARKER, True)
    root.addHandler(store)
    _persist_handlers.append(store)

    # 启动时裁一次：上次进程留下的过期日志不必留到运行时再慢慢淘汰。
    # 这里的异常同样要吞掉——裁剪失败只意味着「暂时多占点空间」，不值得拖住启动。
    try:
        LogRepository.prune(
            retention_days=settings.log_retention_days,
            max_rows=settings.log_max_rows,
        )
    except Exception:  # noqa: BLE001
        _module_logger.warning(
            "日志裁剪失败，将在下次启动时重试",
            exc_info=True,
            extra={"event": "log.prune_failed"},
        )


def shutdown_log_persistence() -> None:
    """摘掉持久化 handler 并 flush 剩余日志。

    进程退出前必须调用：否则最后一批日志还躺在内存队列里就被丢弃了，
    而那批往往正好是「服务为什么退出」的那几行。
    """
    root = logging.getLogger()
    for handler in list(_persist_handlers):
        root.removeHandler(handler)
        handler.close()
    _persist_handlers.clear()


def reset_logging() -> None:
    """撤销 `configure_logging` 的效果，回到「没配置过」的状态。

    主要给测试用：用例之间必须互不影响，否则「前一个用例配置过」会让后一个用例里
    「配置确实生效了」的断言永远通过——那是在测测试顺序，不是在测代码。

    持久化一并撤掉：它带着后台线程，若留着，前一个用例的日志线程会继续往
    **后一个用例的临时库**里写，表现为「凭空多出几行日志」这种极难复盘的串扰。
    """
    shutdown_log_persistence()
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
