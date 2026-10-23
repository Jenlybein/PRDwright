"""澄清对话轮次（`chat_turns`）的数据仓储。

为什么是「轮次表」而不是把消息塞回方案快照
------------------------------------------
对话是**只追加的记录集**：每条轮次独立存在、独立产生、需要按线程查询与按顺序拼接。
把它塞进一个整份覆盖的 JSON 列里，会同时失去三件事：增量写、并发安全、可查。

写者只有一个
------------
本表**只由后端写**（任务收尾 / 创建任务时落输入轮次）。前端不写、也没有接口写——
它只从 `list_thread` 读。这正是「单写者」的实现：两台设备同时打开同一方案时，
它们只会各自订阅同一个任务，不会各自往表里塞一条，因此不存在互相覆盖。

## 形态（v6 / T16.6）

绑定 `Session` 的实例方法；**本类不 commit / rollback / close**。
读方法返回 `ChatTurn` 模型对象；`to_history_item` / `to_display_turn` 是**投影函数**
（模型 → 上游协议形状 / 前端形状），不是数据库访问，留在本模块因为它们只认这一张表。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from backend.core.clock import utc_now_text
from backend.core.models import ChatTurn


def _decode_list(raw: Any) -> list[Any]:
    """把 TEXT 列里的 JSON 数组还原；坏值退化为空数组而不是让整次读取失败。"""
    if not isinstance(raw, str) or not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _decode_dict(raw: Any) -> dict[str, Any] | None:
    """把 TEXT 列里的 JSON 对象还原；坏值退化为 None。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _dumps(value: Any, fallback: str = "[]") -> str:
    """序列化可选结构；`None` / 空值统一写 `fallback`，避免列里出现 'null'。"""
    if value in (None, "", [], {}):
        return fallback
    return json.dumps(value, ensure_ascii=False)


class ChatTurnRepository:
    """`chat_turns` 的读写（绑定一个 `Session`）。所有读方法强制带 `owner_id`。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        owner_id: int,
        solution_id: int,
        *,
        stage: str,
        role: str,
        content: str,
        job_id: int | None = None,
        tool_call_id: str = "",
        tool_result: dict[str, Any] | None = None,
        asks: list[Any] | None = None,
        answers: list[Any] | None = None,
        tool_calls: list[Any] | None = None,
        interrupted: bool = False,
    ) -> int:
        """写入一条轮次，返回其 id（幂等：同一任务的同一角色只写一次）。

        幂等靠 `ux_chat_turns_job_role` 这个唯一索引，而不是靠调用方自觉检查：
        收尾可能被重连触发两次、被取消与正常结束同时撞上，代码路径一多就一定会漏。

        做法是「先查 → 没有才插」，两段在**同一事务**内（调用方的 `session_scope()`）：
        先查把常见的「重复写」变成一次 SELECT（无异常、无 savepoint），
        而并发的两条同时查到「没有」时，唯一索引会在 flush 阶段把后一条挡下来——
        此时回查一次拿到已存在那一行的 id。索引是最后一道门，不是唯一一道。

        Args:
            owner_id: 归属用户。
            solution_id: 方案 id。
            stage: 线程名。
            role: `user` / `assistant` / `tool` / `system`。
            content: 正文。
            job_id: 归属任务；为 `None` 时不参与幂等（`INSERT OR IGNORE` 的原语义：
                部分唯一索引只约束 `job_id IS NOT NULL` 的行）。
            tool_call_id: 工具调用 id（`role='tool'` 时必填，用于与 `tool_calls` 配对）。
            tool_result: 工具结果结构。
            asks: 结构化选择题。
            answers: 用户作答。
            tool_calls: 助手发起、随后的工具应答要配对的那一串。
            interrupted: 该轮是否被用户停止。

        Returns:
            轮次 id；撞唯一索引且回查不到时返回 0（与旧实现同口径）。
        """
        if job_id is not None:
            existing = self._find_id_by_job_role(job_id, role)
            if existing is not None:
                return existing

        turn = ChatTurn(
            user_id=owner_id,
            solution_id=solution_id,
            stage=stage,
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            tool_result=_dumps(tool_result, ""),
            asks=_dumps(asks),
            answers=_dumps(answers),
            tool_calls=_dumps(tool_calls),
            interrupted=bool(interrupted),
            job_id=job_id,
            created_at=utc_now_text(),
        )
        try:
            # 用 SAVEPOINT 而不是直接 flush：并发撞索引时只需要撤销这一次插入，
            # 不能把外层事务一起回滚（那会连带作废同一事务里已经写好的其它东西）。
            with self.session.begin_nested():
                self.session.add(turn)
                self.session.flush()
        except IntegrityError:
            # 并发的同 job 同 role 抢先写入了：唯一索引挡住了我们，回查拿它的 id
            found = self._find_id_by_job_role(job_id, role)
            return found if found is not None else 0
        return int(turn.id or 0)

    def _find_id_by_job_role(self, job_id: int, role: str) -> int | None:
        """按 `(job_id, role)` 查已有轮次的 id（幂等写与冲突回查共用）。"""
        found = (
            self.session.execute(
                select(ChatTurn.id).where(ChatTurn.job_id == job_id, ChatTurn.role == role)
            )
            .scalars()
            .first()
        )
        return int(found) if found is not None else None

    def list_all(self, owner_id: int, solution_id: int) -> list[ChatTurn]:
        """读取某方案**全部线程**的轮次，按发生顺序排列。

        前端把四条线程合并成一个展示数组（按 `stage` 过滤渲染），因此「打开方案」
        需要的是一次取全，而不是四次往返。`stage` 过滤仍然保留在
        `list_thread` 里——按线程取是「翻到某条线程」的粒度，两者用途不同。
        """
        return list(
            self.session.execute(
                select(ChatTurn)
                .where(ChatTurn.solution_id == solution_id, ChatTurn.user_id == owner_id)
                .order_by(ChatTurn.id)
            )
            .scalars()
            .all()
        )

    def list_thread(self, owner_id: int, solution_id: int, stage: str) -> list[ChatTurn]:
        """读取某方案某线程的全部轮次，按发生顺序（`id`）排列。

        排序用主键而不是时间戳：同一次响应可能同时落两条（assistant + 它的工具应答），
        同一毫秒内时间戳并列，谁先谁后就没有答案了。
        """
        return list(
            self.session.execute(
                select(ChatTurn)
                .where(
                    ChatTurn.solution_id == solution_id,
                    ChatTurn.user_id == owner_id,
                    ChatTurn.stage == stage,
                )
                .order_by(ChatTurn.id)
            )
            .scalars()
            .all()
        )

    def list_history(
        self, owner_id: int, solution_id: int, stage: str, *, before_id: int
    ) -> list[ChatTurn]:
        """读取拼上下文用的历史：本线程中、指定轮次**之前**的那些。

        本轮输入在创建任务时已经落库（并拿到 id），因此历史必须是「它之前」的部分，
        否则同一句话会既在历史里、又作为 `user_message` 再出现一次。

        Args:
            before_id: 上界（不含）：只取 id 小于它的轮次。
        """
        return list(
            self.session.execute(
                select(ChatTurn)
                .where(
                    ChatTurn.solution_id == solution_id,
                    ChatTurn.user_id == owner_id,
                    ChatTurn.stage == stage,
                    ChatTurn.id < before_id,
                )
                .order_by(ChatTurn.id)
            )
            .scalars()
            .all()
        )

    def has_turns(self, owner_id: int, solution_id: int, stage: str) -> bool:
        """该线程是否已经有内容（「开新会话是否清空历史」的判据之一）。"""
        found = (
            self.session.execute(
                select(ChatTurn.id)
                .where(
                    ChatTurn.solution_id == solution_id,
                    ChatTurn.user_id == owner_id,
                    ChatTurn.stage == stage,
                )
                .limit(1)
            )
            .scalars()
            .first()
        )
        return found is not None

    def delete_thread(self, owner_id: int, solution_id: int, stage: str) -> int:
        """清空某条线程的轮次，返回删除条数。

        只在「用户明确要求重开这条线程」时调用（需求澄清重新提交表单）。
        删除方案不经过这里：`chat_turns.solution_id` 上是 `ON DELETE CASCADE`，
        级联必须在库里，不能靠调用方记得。
        """
        result = self.session.execute(
            delete(ChatTurn).where(
                ChatTurn.solution_id == solution_id,
                ChatTurn.user_id == owner_id,
                ChatTurn.stage == stage,
            )
        )
        return int(result.rowcount or 0)


def to_history_item(turn: ChatTurn) -> dict[str, Any]:
    """把一条轮次转成 `conversation_service._build_lc_messages` 认识的历史项。

    三个角色各有各的必要字段，少一个就会出现「悬空工具调用」或「上下文重复」：

    - `assistant`：带 `tool_calls` 才能让随后的工具应答配得上对；
    - `tool`：必须带 `tool_call_id`（配对）与 `tool_result`（结构化正文，缺了只能
      退回展示文本）——这两样丢了，上游 API 直接报错；
    - `user` / `system`：只有正文，原样带上。

    `interrupted` 的轮次**照常参与历史**：用户按了停止，那些话确实说过，
    模型该知道；界面上另有标记，不影响上下文。
    """
    role = str(turn.role or "")
    item: dict[str, Any] = {"role": role, "content": str(turn.content or "")}
    tool_calls = _decode_list(turn.tool_calls)
    if role == "assistant" and tool_calls:
        item["tool_calls"] = tool_calls
    if role == "tool":
        tool_call_id = str(turn.tool_call_id or "")
        if tool_call_id:
            item["tool_call_id"] = tool_call_id
        tool_result = _decode_dict(turn.tool_result)
        if tool_result is not None:
            item["tool_result"] = tool_result
    return item


def to_display_turn(turn: ChatTurn) -> dict[str, Any]:
    """把一条轮次转成前端认识的形状（`ChatMessage` 的字段子集）。

    ⚠️ **`tool` 在这里被投影成 `user`。** 前端 `ChatMessage.role` 刻意不含 `tool`：
    它只画气泡，不参与上游协议。让 `tool` 流到前端，会在快照/列表复建时被
    类型白名单整条丢掉——那种不一致不报错，表现为「刷新一次消息就没了」。
    存储形状与展示形状分开，投影只在这一处做。
    """
    role = str(turn.role or "")
    display: dict[str, Any] = {
        "id": int(turn.id or 0),
        "role": "user" if role == "tool" else role,
        "content": str(turn.content or ""),
        "stage": str(turn.stage or ""),
        "created_at": str(turn.created_at or ""),
    }
    asks = _decode_list(turn.asks)
    if asks:
        display["asks"] = asks
    answers = _decode_list(turn.answers)
    if answers:
        display["answers"] = answers
    tool_calls = _decode_list(turn.tool_calls)
    if tool_calls:
        display["tool_calls"] = tool_calls
    if turn.interrupted:
        display["interrupted"] = True
    return display
