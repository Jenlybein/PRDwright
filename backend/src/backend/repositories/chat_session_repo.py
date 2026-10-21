"""对话会话 id（`chat_sessions`）的数据仓储。

这张表只有一个职责：记住**每条线程的后端会话 id**。

为什么它不该继续留在前端快照里
------------------------------
会话 id 是「这条线程在服务端对应哪一次会话」的唯一凭据。它原先存在
`SessionDataV2.conversationIds`（一份按 stage 取的 map）里，于是：

- 换设备时，另一台机器拿着快照里的旧 id 去续聊，而服务端那次会话可能早已消失
  或已被别处的对话推进过——**id 归谁维护，决定了谁有权判断它是否还有效**；
- 四条线程各一个会话，混一条就会出现「模型突然忘了自己在聊什么」，
  而这种故障没有任何报错。

会话归属与轮次归属是同一件事（都属于 `(方案, 线程)`），因此两者放在同一侧：
服务端持有、前端只读。`(solution_id, stage)` 唯一索引保证不会出现第二条。

服务重启后内存里的会话记录会全部丢失，此时 id 仍然有效（它只是一个键），
下一次续聊会拿到 `SESSION_NOT_FOUND`，由对话服务降级为「用历史重建上下文」——
与本表无关，那是对话服务自己的容错。
"""

from __future__ import annotations

from backend.core.db import connect, utc_now_text


class ChatSessionRepository:
    """`chat_sessions` 的读写。所有方法强制带 `owner_id`。"""

    @staticmethod
    def get(owner_id: int, solution_id: int, stage: str) -> str:
        """读取某条线程的会话 id；没有则返回空串（调用方据此走无状态模式）。"""
        with connect() as conn:
            row = conn.execute(
                "SELECT conversation_id FROM chat_sessions "
                "WHERE solution_id = ? AND user_id = ? AND stage = ?",
                (solution_id, owner_id, stage),
            ).fetchone()
            return str(row[0]) if row and row[0] else ""

    @staticmethod
    def set(owner_id: int, solution_id: int, stage: str, conversation_id: str) -> None:
        """写入 / 覆盖某条线程的会话 id（`(solution_id, stage)` 唯一，故用 UPSERT）。

        空串表示「这条线程还没有会话」——此时**删除这一行**而不是存一个空串：
        留下空行会让「有没有会话」多一种表示法，读取侧就得同时判空串与判无行。
        """
        with connect() as conn:
            if not conversation_id:
                conn.execute(
                    "DELETE FROM chat_sessions WHERE solution_id = ? AND user_id = ? "
                    "AND stage = ?",
                    (solution_id, owner_id, stage),
                )
            else:
                conn.execute(
                    "INSERT INTO chat_sessions "
                    "(user_id, solution_id, stage, conversation_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(solution_id, stage) DO UPDATE SET "
                    "conversation_id = excluded.conversation_id, "
                    "updated_at = excluded.updated_at",
                    (owner_id, solution_id, stage, conversation_id, utc_now_text()),
                )
            conn.commit()

    @staticmethod
    def clear_for_solution(owner_id: int, solution_id: int) -> None:
        """清掉某方案下全部会话 id（「整份重开」时用）。"""
        with connect() as conn:
            conn.execute(
                "DELETE FROM chat_sessions WHERE solution_id = ? AND user_id = ?",
                (solution_id, owner_id),
            )
            conn.commit()
