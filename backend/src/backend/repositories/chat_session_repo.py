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

## 形态（v6 / T16.6）

绑定 `Session` 的实例方法；**本类不 commit / rollback / close**。
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from backend.core.clock import utc_now_text
from backend.core.models import ChatSession


class ChatSessionRepository:
    """`chat_sessions` 的读写（绑定一个 `Session`）。所有方法强制带 `owner_id`。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, owner_id: int, solution_id: int, stage: str) -> str:
        """读取某条线程的会话 id；没有则返回空串（调用方据此走无状态模式）。

        Args:
            owner_id: 归属用户。
            solution_id: 方案 id。
            stage: 线程名（四条澄清线程之一）。
        """
        value = (
            self.session.execute(
                select(ChatSession.conversation_id).where(
                    ChatSession.solution_id == solution_id,
                    ChatSession.user_id == owner_id,
                    ChatSession.stage == stage,
                )
            )
            .scalars()
            .first()
        )
        return str(value) if value else ""

    def set(self, owner_id: int, solution_id: int, stage: str, conversation_id: str) -> None:
        """写入 / 覆盖某条线程的会话 id（`(solution_id, stage)` 唯一，故用 UPSERT）。

        空串表示「这条线程还没有会话」——此时**删除这一行**而不是存一个空串：
        留下空行会让「有没有会话」多一种表示法，读取侧就得同时判空串与判无行。

        Args:
            owner_id: 归属用户。
            solution_id: 方案 id。
            stage: 线程名。
            conversation_id: 会话 id；空串表示删除该行。
        """
        if not conversation_id:
            self.session.execute(
                delete(ChatSession).where(
                    ChatSession.solution_id == solution_id,
                    ChatSession.user_id == owner_id,
                    ChatSession.stage == stage,
                )
            )
            return

        # ⚠️ 刻意用「一条 UPSERT」而不是「先查再写」：拆成两步之后，
        # 「两条线程同时写同一行」就成了「两个都查到没有 → 两个都插入」，
        # 唯一索引会把第二条变成 IntegrityError，而那不是业务错误。
        # 冲突目标是唯一索引 `ux_chat_sessions_thread (solution_id, stage)`。
        now = utc_now_text()
        statement = (
            sqlite_insert(ChatSession)
            .values(
                user_id=owner_id,
                solution_id=solution_id,
                stage=stage,
                conversation_id=conversation_id,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["solution_id", "stage"],
                set_={"conversation_id": conversation_id, "updated_at": now},
            )
        )
        self.session.execute(statement)

    def clear_for_solution(self, owner_id: int, solution_id: int) -> None:
        """清掉某方案下全部会话 id（「整份重开」时用）。

        Args:
            owner_id: 归属用户。
            solution_id: 方案 id。
        """
        self.session.execute(
            delete(ChatSession).where(
                ChatSession.solution_id == solution_id,
                ChatSession.user_id == owner_id,
            )
        )
