"""排空：收到终止信号之后，把在途任务收成确定结局。

## 它解决的是哪一种「消失」

任务与浏览器连接是解耦的（见 `job_service`）：刷新只退订，任务照跑。这个设计让
「关掉页面生成继续」成立，代价是**进程退出时没有人在跑收尾**——正在生成的 PRD
连同内存里的运行时一起消失，只留下库里一条 `running`，等下次启动被
`reap_orphans()` 收成失败。用户看到的是「点了生成，回来什么都没有」。

容器化把这件事从「偶尔」（手动重启）变成「每次 `docker compose restart` 都发生」，
所以必须给它一个确定结局。排空的语义分五步，顺序不能换：

1. **摘流**：`begin()` 让 `/readyz` 立刻转 503——这是编排器/反代「别再派活」的唯一信号；
2. **停收新活**：`create_job` 路由在排空期间直接拒绝（503），而不是把请求塞进一个
   马上要死的队列（见 `api/jobs.py`）；
3. **等在途任务最多 N 秒**（`drain_timeout_seconds`，默认 30）——**这是上限不是必等**：
   任务都结束了就立刻返回；
4. **超时者标为已取消**：落终态（`failed` + `CANCELLED`，不新增状态值，前后端契约不动）、
   向 SSE 订阅者发一条 `error` 事件，再补 `run_summary`。订阅者因此拿到的是
   「被取消」而不是「完成」，也不会永远挂着等一个再也不会来的事件；
5. **flush 日志再退出**（`shutdown()`）：日志队列里最后那几行往往正好是
   「这次重启取消了几个任务」。

## 为什么做成「可以直接调用的单元」

信号是操作系统的东西：它只在进程真的被杀时发生，且 Linux 有、Windows 的事件循环
没有（`loop.add_signal_handler` 在 Proactor 上直接抛 `NotImplementedError`）。
把排空写进信号处理函数里，等于让这条最要紧的收尾路径**只有在生产环境才能被验证**。

所以这里把「排空」与「信号」拆开：本模块只提供可被任何调用方（信号处理函数、
lifespan 的关闭阶段、用例）直接调用的 `drain_controller`；信号只负责调用它。
用例因此能在毫秒级验证「给定在途任务，排空后它被标成已取消并落了终态」，
不需要子进程，也不需要真信号。

## 与「取消」的关系

`cancelled=True` 是既有的协作式取消标记（用户点停止、删方案、删账号都走它）：
`run_job` 在下一个事件处跳出循环，由 `_finalize` 收尾。排空复用它，而不是发明一套
新的取消机制——「已取消」只该有一种表示，否则前端与排查要看两处。

差别只在**超时者不能等它自己跳出循环**：它可能正卡在模型调用里。

「等待在途任务」这件事归生成任务一侧（`services/job_wait.py`）：本模块只问
「还有哪些在跑」「要不要等」「超时取消哪几支」，一个运行时内部字段都不碰。
等待满窗口之后，由那一侧替超时任务把终态写完（发错误事件 → 收尾落终态 →
给订阅者放哨兵）——那个卡住的任务随后即使再产事件也不会改写这个结论。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass

from backend.core.config import get_settings
from backend.core.logging_setup import shutdown_log_persistence
from backend.services import job_wait

logger = logging.getLogger(__name__)

# 超时被取消的任务落进 `error_code` 的值：与用户点停止、删方案共用 `CANCELLED`
# （既有口径，前端按它区分「不是红色报错的已取消」）。文案要能说明**是谁**动的手：
# 用户看到「方案已删除」会以为数据没了，运维看到「用户停止」会以为是自己人点的。
DRAIN_CANCEL_MESSAGE = "服务正在重启，任务已取消（重启前有 30 秒宽限，它没能在窗口内跑完）"


@dataclass(frozen=True)
class DrainReport:
    """一次排空的结果（给日志、用例与人看）。

    Attributes:
        in_flight: 排空开始时还在跑的任务数。
        finished: 在窗口内自然结束的任务数（正常走完，终态由 `_finalize` 决定）。
        cancelled: 超时被取消的任务数——就是「这次重启影响了几个用户」。
        waited_seconds: 实际等了多久（上限是配置值；没任务时接近 0）。
        hit_deadline: 是否撞到了窗口上限（有任务被取消即为 `True`）。
    """

    in_flight: int = 0
    finished: int = 0
    cancelled: int = 0
    waited_seconds: float = 0.0
    hit_deadline: bool = False


class DrainController:
    """排空的状态机与执行者（进程内单例，见模块末尾的 `drain_controller`）。

    为什么需要状态而不只是一个函数：「排空期间就绪探针返回 503」与「排空期间拒绝
    新任务」都是**跨越一段异步时间**的状态，得有个地方记住它。这个状态只有一个
    方向（未排空 → 排空中），复位只发生在进程/应用启动时（`reset()`）。
    """

    def __init__(self) -> None:
        """初始为「未排空」。"""
        self._draining = False
        # 信号处理器只装一次（重复装会把上一个覆盖掉，而它们会争着 create_task）
        self._handlers_installed = False

    # -- 状态 -----------------------------------------------------------

    def is_draining(self) -> bool:
        """是否正在排空（`/readyz` 与创建任务的路由都问这一句）。"""
        return self._draining

    def reset(self) -> None:
        """把状态清回「未排空」。

        调用点只有 `main.py` 的 lifespan **启动**处：一个刚起来的进程当然没在排空。
        有了它，「同一进程里起多次应用」（用例里的 TestClient、`--reload`）才不会
        继承上一次排空的 503。信号处理器标记一并清掉：新应用跑在**另一个事件循环**
        上，旧的那一套装着也不算数。
        """
        self._draining = False
        self._handlers_installed = False

    def begin(self) -> None:
        """摘流：就绪探针从这一刻起返回 503。幂等（重复调用只记一次日志）。"""
        if self._draining:
            return
        self._draining = True
        settings = get_settings()
        logger.warning(
            "开始排空：就绪探针转 503，停止接收新任务，等在途任务最多 %s 秒",
            settings.drain_timeout_seconds,
            extra={
                "event": "service.drain_started",
                "timeout_seconds": settings.drain_timeout_seconds,
                "in_flight": len(self.in_flight()),
            },
        )

    # -- 排空 -----------------------------------------------------------

    async def drain(self, *, timeout: float | None = None) -> DrainReport:
        """摘流 → 等在途任务最多 `timeout` 秒 → 超时者取消落终态。

        Args:
            timeout: 等待上限（秒）。`None` 取 `settings.drain_timeout_seconds`（默认 30）。
                用例传小值来避免真等 30 秒；生产走配置，因此部署与测试都不必改代码。

        Returns:
            本次排空的汇总。**永远正常返回**：排空是收尾动作，它自己失败不应该
            让进程以非零码退出（那样编排器会把一次干净的重启记成崩溃）。
        """
        self.begin()
        limit = get_settings().drain_timeout_seconds if timeout is None else timeout
        started = time.monotonic()

        # 三个问题一次交回生成任务一侧：还有哪些在跑（这份句柄快照）、要不要等、
        # 超时取消哪几支。本模块读到的只是带 job_id 的抽象句柄，不碰运行时对象、
        # 任务对象或状态字段；等待上限（默认 30 秒）与取消原因仍由排空决定。
        outcome = await job_wait.wait_for_in_flight(
            self.in_flight(), timeout=limit, reason=DRAIN_CANCEL_MESSAGE
        )

        report = DrainReport(
            in_flight=outcome.in_flight,
            finished=outcome.finished,
            cancelled=outcome.cancelled,
            waited_seconds=time.monotonic() - started,
            hit_deadline=outcome.hit_deadline,
        )
        # 汇总一行：运维要能一眼看出「这次重启影响了几个用户」。
        # 事件名与字段和 `service.drain_started` 配对，日志页按 request/事件名能连起来。
        logger.warning(
            "排空完成：等了 %.1f 秒，在途 %d 个任务，自然结束 %d 个，取消 %d 个",
            report.waited_seconds,
            report.in_flight,
            report.finished,
            report.cancelled,
            extra={
                "event": "service.drain_finished",
                "in_flight": report.in_flight,
                "finished_jobs": report.finished,
                "cancelled_jobs": report.cancelled,
                "waited_seconds": round(report.waited_seconds, 3),
                "timeout_seconds": limit,
            },
        )
        return report

    async def shutdown(self, *, timeout: float | None = None) -> int:
        """终止信号之后的**全部**收尾：排空 → flush 日志 → 交出退出码。

        返回 `0`：这是「正常收尾」的退出码，而不是被信号打断的 `128+15`。
        本函数**自己不结束进程**（那样用例就没法调它了）——调用方拿到码之后
        才 `os._exit(code)`，见 `install_signal_handlers()` 的说明。
        """
        await self.drain(timeout=timeout)
        # 最后一批日志还躺在内存队列里，而它们往往正好是「服务为什么退出」的那几行
        # （含上面那行排空汇总）。lifespan 的关闭阶段在这条路径上不会走到——
        # 我们接管了信号，进程随后就退出——所以必须在这里显式 flush。
        shutdown_log_persistence()
        logger.info(
            "排空结束，进程以成功状态退出",
            extra={"event": "service.drain_exit", "exit_code": 0},
        )
        return 0

    # -- 信号（只是一层薄适配） -------------------------------------------

    def install_signal_handlers(self) -> bool:
        """把 `SIGTERM` / `SIGINT` 接到排空上；装不上就返回 `False`（绝不抛）。

        **为什么必须自己接管**：uvicorn 自己的处理器收到信号就立刻开始关监听、随后跑
        lifespan 的关闭阶段。那时**监听已经关了**，就绪探针根本不再应答——而「摘流」
        恰恰要求它在排空期间还能返回 503。只有抢在 uvicorn 之前把信号接过来，
        才能做到「先转 503、再等任务、最后才关」。

        **为什么容忍装不上**：Windows 的事件循环（Proactor）没有信号接口
        （`add_signal_handler` 直接抛 `NotImplementedError`），非主线程也不行
        （`set_wakeup_fd` 抛 `ValueError`，用例里的 TestClient 正是这样）。
        这些环境下排空仍有兜底：lifespan 的关闭阶段会再调一次 `drain()`。

        Returns:
            是否装上了。
        """
        if self._handlers_installed:
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 没有运行中的事件循环（同步调用方 / 用例）
            return False

        installed: list[int] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                # 先摘掉服务器自己装的那一个：两套处理器同时生效时，uvicorn 会在
                # `should_exit` 之后立刻关监听，把我们要等的那段窗口整个抹掉。
                loop.remove_signal_handler(sig)
                loop.add_signal_handler(sig, self._on_termination, sig, loop)
            except (NotImplementedError, ValueError, RuntimeError):
                # 一个装不上就两个都别装：留下「SIGTERM 走排空、Ctrl+C 走 uvicorn」的
                # 半套状态，会让本地调试与容器里的行为悄悄不一致。
                for done in installed:
                    loop.remove_signal_handler(done)
                return False
            installed.append(sig)

        self._handlers_installed = True
        logger.info(
            "终止信号已接到排空",
            extra={"event": "service.drain_signals_installed", "signals": installed},
        )
        return True

    def _on_termination(self, signum: int, loop: asyncio.AbstractEventLoop) -> None:
        """信号回调：摘流 + 排空 + 退出（这里**不许**抛异常，否则收尾就断了）。"""
        if self._draining:
            # 第二次终止信号：操作者等不及了（第一次已经摘过流、也发过日志）。
            # 与 uvicorn 的「再按一次强制退出」同一约定——否则排空卡住时只能等 SIGKILL。
            logger.warning(
                "排空中又收到终止信号，立即退出",
                extra={"event": "service.drain_forced_exit", "signal": signum},
            )
            os._exit(0)
        logger.warning(
            "收到终止信号，开始排空",
            extra={"event": "service.drain_signal", "signal": signum},
        )
        loop.create_task(self._drain_then_exit())

    async def _drain_then_exit(self) -> None:
        """排空走完就结束进程。

        ⚠️ 为什么是 `os._exit` 而不是让 uvicorn 自己收尾：上面把 uvicorn 的处理器摘掉了，
        它的 `should_exit` 再也不会被置位；而进程内拿不到那个 `Server` 对象（它由
        `uvicorn.run` 的局部变量持有）。代价说清楚——`os._exit` 跳过 atexit 与连接清理，
        所以它必须是**最后一步**：排空把在途任务收成终态、`shutdown()` 已经把日志队列
        flush 干净。剩下可能被打断的只有正在读响应的连接，而那一刻服务已经在摘流
        （`/readyz` 已 503 一段时间），且宿主的 `stop_grace_period` 兜着最坏情况。
        """
        code = await self.shutdown()
        os._exit(code)

    # -- 内部 -----------------------------------------------------------

    def in_flight(self) -> list[job_wait.InFlightHandle]:
        """还没跑完的任务（抽象句柄快照）。

        判据与「谁在读登记表」都归生成任务一侧：本模块只调 `job_wait.in_flight()`，
        拿到的是带 job_id 的句柄，不是运行时对象——`main.py` 用它判断「要不要兜底
        排空」，`begin()` 用它数「开始时在途几支」，两者都不需要内部字段。
        """
        return job_wait.in_flight()


# 模块级单例：进程内只有一份排空状态（与 `job_bus` / 运行时登记表同一口径）
drain_controller = DrainController()
