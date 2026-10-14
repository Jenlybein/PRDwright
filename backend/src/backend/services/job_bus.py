"""生成任务的内存事件总线。

把「同进程的任务产出」与「任意数量的浏览器订阅」解耦：任务只管往总线 `publish`，
每个订阅者拿到自己的无界队列，互不阻塞。刷新 / 断线只会让某个订阅者退出，
任务本身不受影响——这正是本期「生成与连接解耦」的核心。

设计取舍：
- **队列无界（`maxsize=0`）**：绝不 drop-oldest。丢一个 `chunk` 会破坏「`part` 拼接
  == `reply`」这条契约（前端预览会缺一段），因此宁可占用内存也不丢事件。
- `publish` 是**同步、非阻塞**的：任务在事件循环里逐事件推进，若此处 `await`，
  一个慢订阅者就会拖慢整个生成。无界队列保证 `put_nowait` 永不阻塞。
- `close` 给每个订阅者放一个哨兵 `None`，读端见到它即退出循环；任务结束后订阅者
  不会被永久挂起。
"""

from __future__ import annotations

import asyncio
from typing import Any

# 订阅结束的哨兵：读端见到它即 `return`
CLOSE_SENTINEL: None = None


class JobBus:
    """按 `job_id` 分组的发布 / 订阅（仅进程内，单实例）。"""

    def __init__(self) -> None:
        """初始化空的总线（无任何订阅者）。"""
        self._subscribers: dict[int, set[asyncio.Queue]] = {}

    def subscribe(self, job_id: int) -> asyncio.Queue:
        """为某任务新增一个订阅者，返回其专属的**无界**队列。

        必须在事件循环内调用（`asyncio.Queue` 绑定当前 loop）。
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=0)
        self._subscribers.setdefault(job_id, set()).add(queue)
        return queue

    def unsubscribe(self, job_id: int, queue: asyncio.Queue) -> None:
        """移除一个订阅者（幂等：重复移除不报错）。"""
        subscribers = self._subscribers.get(job_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(job_id, None)

    def publish(self, job_id: int, event: Any) -> None:
        """把事件推送给该任务的全部订阅者。

        **同步、非阻塞**：逐个 `put_nowait` 到无界队列，因此不会阻塞、也不会丢事件。
        无订阅者时是 no-op（事件就此丢弃，由「重连先读快照」补全已产出的正文）。
        """
        for queue in list(self._subscribers.get(job_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - 无界队列不会触发
                continue

    def close(self, job_id: int) -> None:
        """结束某任务：给每个订阅者放哨兵 `None`，然后清空该任务的订阅表。"""
        subscribers = self._subscribers.pop(job_id, None)
        if not subscribers:
            return
        for queue in subscribers:
            try:
                queue.put_nowait(CLOSE_SENTINEL)
            except asyncio.QueueFull:  # pragma: no cover
                continue


# 模块级单例：进程内唯一的任务事件总线
job_bus = JobBus()
