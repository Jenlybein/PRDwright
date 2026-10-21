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
"""

from __future__ import annotations

import json
from typing import Any

from backend.core.db import connect, utc_now_text


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
    """`chat_turns` 的读写。所有读方法强制带 `owner_id`。"""

    @staticmethod
    def create(
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
        `INSERT OR IGNORE` 之后回查一行，因此「第一次写」与「重复写」拿到同一个 id。
        """
        with connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO chat_turns "
                "(user_id, solution_id, stage, role, content, tool_call_id, tool_result, "
                " asks, answers, tool_calls, interrupted, job_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    owner_id,
                    solution_id,
                    stage,
                    role,
                    content,
                    tool_call_id,
                    _dumps(tool_result, ""),
                    _dumps(asks),
                    _dumps(answers),
                    _dumps(tool_calls),
                    1 if interrupted else 0,
                    job_id,
                    utc_now_text(),
                ),
            )
            conn.commit()
            if cur.rowcount == 1 and cur.lastrowid:
                return int(cur.lastrowid)
            # 撞上唯一索引：返回已有那一行，调用方无需区分「新建」与「已存在」
            row = conn.execute(
                "SELECT id FROM chat_turns WHERE job_id = ? AND role = ?",
                (job_id, role),
            ).fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    def list_all(owner_id: int, solution_id: int) -> list[dict[str, Any]]:
        """读取某方案**全部线程**的轮次，按发生顺序排列。

        前端把四条线程合并成一个展示数组（按 `stage` 过滤渲染），因此「打开方案」
        需要的是一次取全，而不是四次往返。`stage` 过滤仍然保留在
        `list_thread` 里——按线程取是「翻到某条线程」的粒度，两者用途不同。
        """
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_turns WHERE solution_id = ? AND user_id = ? "
                "ORDER BY id ASC",
                (solution_id, owner_id),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def list_thread(owner_id: int, solution_id: int, stage: str) -> list[dict[str, Any]]:
        """读取某方案某线程的全部轮次，按发生顺序（`id`）排列。

        排序用主键而不是时间戳：同一次响应可能同时落两条（assistant + 它的工具应答），
        同一毫秒内时间戳并列，谁先谁后就没有答案了。
        """
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_turns WHERE solution_id = ? AND user_id = ? "
                "AND stage = ? ORDER BY id ASC",
                (solution_id, owner_id, stage),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def list_history(
        owner_id: int, solution_id: int, stage: str, *, before_id: int
    ) -> list[dict[str, Any]]:
        """读取拼上下文用的历史：本线程中、指定轮次**之前**的那些。

        本轮输入在创建任务时已经落库（并拿到 id），因此历史必须是「它之前」的部分，
        否则同一句话会既在历史里、又作为 `user_message` 再出现一次。
        """
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_turns WHERE solution_id = ? AND user_id = ? "
                "AND stage = ? AND id < ? ORDER BY id ASC",
                (solution_id, owner_id, stage, before_id),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def has_turns(owner_id: int, solution_id: int, stage: str) -> bool:
        """该线程是否已经有内容（「开新会话是否清空历史」的判据之一）。"""
        with connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM chat_turns WHERE solution_id = ? AND user_id = ? "
                "AND stage = ? LIMIT 1",
                (solution_id, owner_id, stage),
            ).fetchone()
            return row is not None

    @staticmethod
    def delete_thread(owner_id: int, solution_id: int, stage: str) -> int:
        """清空某条线程的轮次，返回删除条数。

        只在「用户明确要求重开这条线程」时调用（需求澄清重新提交表单）。
        删除方案不经过这里：`chat_turns.solution_id` 上是 `ON DELETE CASCADE`，
        级联必须在库里，不能靠调用方记得。
        """
        with connect() as conn:
            cur = conn.execute(
                "DELETE FROM chat_turns WHERE solution_id = ? AND user_id = ? AND stage = ?",
                (solution_id, owner_id, stage),
            )
            conn.commit()
            return int(cur.rowcount)


def to_history_item(row: dict[str, Any]) -> dict[str, Any]:
    """把一行轮次转成 `conversation_service._build_lc_messages` 认识的历史项。

    三个角色各有各的必要字段，少一个就会出现「悬空工具调用」或「上下文重复」：

    - `assistant`：带 `tool_calls` 才能让随后的工具应答配得上对；
    - `tool`：必须带 `tool_call_id`（配对）与 `tool_result`（结构化正文，缺了只能
      退回展示文本）——这两样丢了，上游 API 直接报错；
    - `user` / `system`：只有正文，原样带上。

    `interrupted` 的轮次**照常参与历史**：用户按了停止，那些话确实说过，
    模型该知道；界面上另有标记，不影响上下文。
    """
    role = str(row.get("role") or "")
    item: dict[str, Any] = {"role": role, "content": str(row.get("content") or "")}
    tool_calls = _decode_list(row.get("tool_calls"))
    if role == "assistant" and tool_calls:
        item["tool_calls"] = tool_calls
    if role == "tool":
        tool_call_id = str(row.get("tool_call_id") or "")
        if tool_call_id:
            item["tool_call_id"] = tool_call_id
        tool_result = _decode_dict(row.get("tool_result"))
        if tool_result is not None:
            item["tool_result"] = tool_result
    return item


def to_display_turn(row: dict[str, Any]) -> dict[str, Any]:
    """把一行轮次转成前端认识的形状（`ChatMessage` 的字段子集）。

    ⚠️ **`tool` 在这里被投影成 `user`。** 前端 `ChatMessage.role` 刻意不含 `tool`：
    它只画气泡，不参与上游协议。让 `tool` 流到前端，会在快照/列表复建时被
    类型白名单整条丢掉——那种不一致不报错，表现为「刷新一次消息就没了」。
    存储形状与展示形状分开，投影只在这一处做。
    """
    role = str(row.get("role") or "")
    display: dict[str, Any] = {
        "id": int(row.get("id") or 0),
        "role": "user" if role == "tool" else role,
        "content": str(row.get("content") or ""),
        "stage": str(row.get("stage") or ""),
        "created_at": str(row.get("created_at") or ""),
    }
    asks = _decode_list(row.get("asks"))
    if asks:
        display["asks"] = asks
    answers = _decode_list(row.get("answers"))
    if answers:
        display["answers"] = answers
    tool_calls = _decode_list(row.get("tool_calls"))
    if tool_calls:
        display["tool_calls"] = tool_calls
    if row.get("interrupted"):
        display["interrupted"] = True
    return display
