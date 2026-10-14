"""测试用的假 LLM。

`ScriptedLLM` 覆盖三条边角：① 一进流就抛（连接失败）；② 末块带 `usage_metadata`
（为后续 token 计量预留）；③ 先吐若干块再抛（生成中途断流）。

**不改动**既有 `test_prd_section_stream.py::FakeLLM` 与 `test_stream_reply.py` 的假 LLM——
那些测试守的是各自的契约，本文件只服务 generation_jobs 的测试。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from langchain_core.messages import AIMessageChunk, BaseMessage

# 断流模拟统一用的异常类型
STREAM_ERROR = ConnectionError("上游连接中断")


class ScriptedLLM:
    """按预设分块作答的假 LLM（满足 `StreamingChatModel` 协议）。

    每块之间 `await asyncio.sleep(0)`：真实 LLM 在等网络，产出与消费天然交错；
    假 LLM 若一路同步跑到底，后台任务会在「消费者第一次 await」时原子跑完，
    测不出「断线途中任务仍在跑」这类行为。主动让出控制权以贴近真实节奏。

    Args:
        pieces: 依次吐出的文本块。
        raise_at: 在第 `raise_at` 块**之前**抛出 `ConnectionError`（`0` 表示一进流就抛）。
            为 `None` 表示正常流完。
        usage_metadata: 末块附带的用量元数据（T1 计量预留）。
    """

    def __init__(
        self,
        pieces: list[str] | None = None,
        *,
        raise_at: int | None = None,
        usage_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._pieces = list(pieces or [])
        self._raise_at = raise_at
        self._usage = usage_metadata

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        """异步流式产出；按 `raise_at` 在指定位置中断。"""
        if self._raise_at == 0:
            raise STREAM_ERROR
        last = len(self._pieces) - 1
        for index, piece in enumerate(self._pieces):
            if self._raise_at is not None and index == self._raise_at:
                raise STREAM_ERROR
            # 让出控制权：给消费者机会在「任务尚未跑完」时读到事件
            await asyncio.sleep(0)
            extra: dict[str, Any] = {}
            if self._usage is not None and index == last:
                extra["usage_metadata"] = self._usage
            yield AIMessageChunk(content=piece, **extra)
