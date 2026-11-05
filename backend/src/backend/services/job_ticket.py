"""生成任务单的两段不可变形状与三步公开动作（票 #36）。

规格定的最终形状
----------------
```
造单（公开动作、不落库/不顶替/不发事件/不做方案校验，允许读技能包清单）
  原始需求 + ①事件流来源 + ④配置/供应商快照 + ③技能包身份(算好的数据)
  → 单①：无 id、不可变
登记（一个事务：当场查 ②方案还在不在、顶替旧任务双标记、落库、写 chat 输入轮次）
  → 单②：带 id 与四个 chat 派生字段、不可变（新对象）
派发/订阅/收尾/排空：只认单②
```

依赖分类（规格「依赖分类」段）：只有「生成事件流来源」与「配置/供应商快照」
是造单时传入的**运行时环境**；「技能包身份」是造单时算好的**数据**随单走；
「方案是否仍存在」是登记与收尾的**当场查询**，不冻结在单上。

本模块是生成任务的公开正门：造单、登记、派发按新职责命名，旧入口与再导出
已删、不设兼容期；`parse_usage` 归 `job_snapshot`、`JobRuntime` 归 `job_runtime`。
包外只经三步公开动作与 `job_service` 的有限动作进入，内部实现名不越过接口。

为什么两张单都是 frozen
-----------------------
「id 落库后才产生」与「不可变」要同时成立，就只能由登记产出**另一张新单**；
单①在登记成功后不再被任何代码持有。嵌套容器也用只读视图 / 元组封住，避免
「单对象冻结了、里面的 dict 仍能改」——那等于第二份真相。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from backend.services import job_service_impl
from backend.services.job_product_kind import _target_product_kind

# 「生成事件流来源」：环境侧给的函数，签名与 `job_service_impl._build_event_stream` 一致。
EventStreamSource = Callable[[str, Mapping[str, Any]], Any]


def _freeze_object(value: Any) -> Any:
    """把不可变数据深冻结：dict → 只读映射、list → 元组、其余标量原样。"""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_object(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_object(item) for item in value)
    return value


def thaw_object(value: Any) -> Any:
    """把深冻结数据递归还原成普通 dict / list（单 → 外部 API 的统一出口）。

    单①/单② 本身仍保持 frozen + 深冻结：这里的还原**只**发生在跨越边界、
    把单里的数据递给外部 API 的那一刻——事件流要 list 才能判断
    `isinstance(sections, list)`，DB 投影与 chat 轮次要普通容器才能 `json.dumps`，
    计量器与回执同理。浅还原（只 `dict(...)` 一层）会让嵌套的只读映射/元组
    漏进这些 API：轻则提示词静默丢字段，重则登记当场抛 `TypeError`。
    """
    if isinstance(value, Mapping) or hasattr(value, "items"):
        return {key: thaw_object(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_object(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class JobTicket:
    """单①（造单产物）：不含 id 的不可变任务单。

    持有原始需求与三类定格信息：事件流来源（环境）、配置/供应商快照（环境）、
    技能包身份（造单时算好的数据）。**没有** `job_id` / `input_turn_id`；
    「方案是否仍存在」不在这里，那是登记与收尾的当场查询。

    造单不落库、不顶替、不发事件；允许读技能包清单（`describe_kind` 链）。
    """

    owner_id: int
    solution_id: int | None
    kind: str
    request_params: Mapping[str, Any]
    event_stream_source: EventStreamSource | None
    config_snapshot: Mapping[str, Any]
    skill_refs: Sequence[Mapping[str, Any]]
    # 配置对象本体（供 `use_config` 冻结到任务上下文）：与 config_snapshot 同源。
    runtime_config: Any = None
    input_fingerprint: str | None = None
    doc_kind: str = ""
    chat_stage: str = ""
    chat_mode: str = "continue"
    chat_content: str = ""
    chat_tool_result: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RegisteredJobTicket:
    """单②（登记产物）：带 id 与四个 chat 派生字段的不可变新单，唯一权威。

    四个 chat 派生字段——`job_id`、`input_turn_id`、`owner_id`、`solution_id`
    ——明写在本单上；不再原地改写调用方传入的 dict。派发及之后只认这一张单。
    """

    job_id: int
    input_turn_id: int
    owner_id: int
    solution_id: int | None
    kind: str
    request_params: Mapping[str, Any]
    event_stream_source: EventStreamSource | None
    config_snapshot: Mapping[str, Any]
    skill_refs: Sequence[Mapping[str, Any]]
    runtime_config: Any = None
    chat_stage: str = ""


def build_job_ticket(
    *,
    owner_id: int,
    solution_id: int | None,
    kind: str,
    request: Mapping[str, Any],
    event_stream_source: EventStreamSource | None = None,
    config: Any | None = None,
    input_fingerprint: str | None = None,
) -> JobTicket:
    """造单：公开正门，产出不含 id 的不可变单①。

    不落库、不顶替旧任务、不发事件、不做「方案是否仍存在」校验；只允许读
    技能包清单（`describe_kind` 链）。运行时环境只有两项：「生成事件流来源」
    与「配置/供应商快照」；生产给真值、测试给假值，都在这一步传入。

    「技能包身份」在造单时**算好作为数据**随单走：复用 #34 的
    `_immutable_projection`，它与 DB 投影、供应商四格、`RunMeter` 四格是同一份值；
    生成中途改后台配置/模板都不会让这一单漂移。
    """
    params = dict(request or {})
    # 配置/供应商快照在造单时一次定格：不给就按生产真值取一份。
    # 这一份同时喂 DB 投影、`runtime.config` 与 `RunMeter` 四格，派发侧不再各取一次。
    from backend.core import runtime_config as rc

    frozen_config = config if config is not None else rc.get_runtime_config()
    projection = job_service_impl._immutable_projection(  # noqa: SLF001 - 包内互引
        kind, params, config=frozen_config
    )
    if event_stream_source is None:
        event_stream_source = job_service_impl._build_event_stream  # noqa: SLF001 - 生产真值

    stage = str(params.get("stage") or "") if kind == "chat" else ""
    tool_result = params.get("tool_result")
    if not isinstance(tool_result, dict):
        tool_result = None
    return JobTicket(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        request_params=_freeze_object(params),
        event_stream_source=event_stream_source,
        config_snapshot=_freeze_object(projection["config_snapshot"]),
        skill_refs=_freeze_object(projection["skill_refs"]),
        runtime_config=frozen_config,
        # 输入指纹是路由层算好的**数据**（仅 prd 有值），登记时随单落列。
        input_fingerprint=input_fingerprint,
        doc_kind=_target_product_kind(kind, params) or "",
        chat_stage=stage,
        chat_mode=str(params.get("chat_mode") or "continue"),
        chat_content=str(params.get("content") or ""),
        chat_tool_result=_freeze_object(tool_result) if tool_result is not None else None,
    )


def register_job_ticket(ticket: JobTicket) -> RegisteredJobTicket:
    """登记：一个事务里查方案、顶替双标记、落库投影、写 chat 输入轮次，产出单②。

    事务边界沿用 `session_scope()`：中途失败时单①丢弃、不产生单②、不派发、
    不留半截 running 行。内存里旧运行时的顶替标记在事务**提交之后**才置，
    失败时不会留下「协程停了、库里还在跑」的错位（与登记事务同一口径）。
    """
    from backend.services.job_registration import register_in_transaction

    return register_in_transaction(ticket)


def start_registered_job(ticket: RegisteredJobTicket) -> None:
    """派发：只认单②，起后台任务推进生成（不阻塞调用方）。

    `create_task` → 赋 `runtime.task` → 登记 `_RUNTIMES` → 加 `_TASKS` 全部在
    任何 `await` 之前完成；`run_job` 的 `use_meter` / `use_config` 包裹顺序、
    `job_bus.publish` 同步广播都不变。配置与技能包身份取单②定格的那一份，
    派发侧不再自行 `get_runtime_config()` 各取一次。
    """
    from backend.services.job_registration import dispatch_registered

    if not isinstance(ticket, RegisteredJobTicket):
        raise TypeError("派发只接受登记后的任务单（单②）")
    dispatch_registered(ticket)


def as_event_request(ticket: RegisteredJobTicket) -> dict[str, Any]:
    """把单②摊平成事件流认识的 request dict（含四个 chat 派生字段）。

    每次调用都造一份**新 dict**：事件流只读它，不会把派生字段写回单②，
    也不会让已发布的事件持有任务单里那份只读映射。
    """
    params = thaw_object(ticket.request_params)
    if ticket.kind == "chat":
        params["input_turn_id"] = ticket.input_turn_id
        params["job_id"] = ticket.job_id
        params["owner_id"] = ticket.owner_id
        params["solution_id"] = ticket.solution_id
    return params


def with_registered_identity(
    ticket: JobTicket, *, job_id: int, input_turn_id: int
) -> RegisteredJobTicket:
    """由单①造出单②（新对象）：不可变数据逐格复制，只补登记产生的身份。"""
    data = {item.name: getattr(ticket, item.name) for item in fields(ticket)}
    data.pop("input_fingerprint", None)
    data.pop("doc_kind", None)
    data.pop("chat_mode", None)
    data.pop("chat_content", None)
    data.pop("chat_tool_result", None)
    return RegisteredJobTicket(job_id=job_id, input_turn_id=input_turn_id, **data)


__all__ = [
    "EventStreamSource",
    "JobTicket",
    "RegisteredJobTicket",
    "as_event_request",
    "build_job_ticket",
    "register_job_ticket",
    "start_registered_job",
    "thaw_object",
    "with_registered_identity",
]
