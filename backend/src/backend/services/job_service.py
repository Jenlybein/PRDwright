"""生成任务服务的公开面：只露有限个正常动作（票 #40）。

包外调用方（启动流程、接口层、其它服务、测试）只经本模块进入生成任务：
`from backend.services import job_service` 取到的名字由 `__all__` 收口，内部
实现住在同目录的 `job_service_impl` 与 `job_*` 模块里，包内互引私有名合法、
包外看不见。新三步「造单 → 登记 → 派发」住在 `job_ticket` / `job_registration`
的公开面；「等待在途」住在 `job_wait`；排空 `job_drain` 留在包外，只经 `job_wait`
的抽象句柄问「还有哪些在跑 / 要不要等 / 超时取消哪几支」。

旧入口 `create_job` / `dispatch` 与整块再导出已删，不设兼容期；
`parse_usage` 归 `job_snapshot`、`JobRuntime` 归 `job_runtime`，
调用方从它们真正所属的地方引用。流式事件形状、错误码、排空语义、
失败不覆写、被取代与已取消分开等既有行为一条都不变。
"""

from backend.services.job_service_impl import (
    cancel_for_user,
    cancel_in_flight,
    cancel_job,
    delete_solution_and_cancel_jobs,
    get_job,
    list_jobs,
    open_stream,
    reap_orphans,
    running_runtimes,
)

__all__ = [
    "reap_orphans",
    "cancel_for_user",
    "cancel_job",
    "list_jobs",
    "get_job",
    "open_stream",
    "delete_solution_and_cancel_jobs",
    "cancel_in_flight",
    "running_runtimes",
]
