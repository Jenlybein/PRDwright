"""火山方舟（Volcano Ark）聊天模型封装。

通过官方 `arkruntime` SDK 调用豆包等模型，对外提供 LangChain `BaseChatModel`
的统一接口（`astream` / `invoke` / `bind_tools` 等），逐块产出 `AIMessageChunk`，
使上游 `conversation_service` / `stream_reply` 无需感知底层 SDK 的差异。

设计要点：
- **子类化 `BaseChatModel`**（不再是普通类）：白拿统一入口、消息归一化、以及**流式
  `tool_call_chunks` → `tool_calls` 的自动拼接**（`AIMessageChunk.__add__` 负责）。
  `bind_tools` 需自己覆写（基类是 `raise NotImplementedError` 占位）。
- 方舟的豆包系列是「混合思考模型」，默认会先吐一大段 `reasoning_content` 再出正文，
  造成首字极慢、正文爆发的观感。这里默认下发 `thinking={"type": "disabled"}` 关闭官方
  思考、直接回答；是否开启由 `thinking_enabled` 控制（默认关闭）。
- 流式模式默认下发 `stream_options={"include_usage": True}`，把 token 用量放在**最后一个
  分块**的顶层 `usage` 上，再映射成 LangChain 的 `usage_metadata`（`input_tokens` /
  `output_tokens` / `total_tokens`），使上游无需区分供应商即可计量。注意该分块的
  `choices` 为空，读取顺序不能颠倒（详见 `_astream` 内的说明）。
- 消息序列化：`_message_to_ark` 除了正文，还把 `AIMessage.tool_calls` 与
  `ToolMessage.tool_call_id` 序列化进请求体——这是「作答侧 tool result 回灌」的前置能力。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from arkruntime import AsyncArk
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCallChunk,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict

# 方舟接受的消息角色，按 LangChain 消息类型映射
_ROLE_MAP: dict[type[BaseMessage], str] = {
    SystemMessage: "system",
    HumanMessage: "user",
    AIMessage: "assistant",
    ToolMessage: "tool",
}


def _role_of(message: BaseMessage) -> str:
    """把 LangChain 消息映射到方舟角色；未知类型兜底为 user。"""
    for message_type, role in _ROLE_MAP.items():
        if isinstance(message, message_type):
            return role
    return "user"


def _content_of(message: BaseMessage) -> str:
    """提取消息正文文本，兼容字符串与多模态 `list[dict]` 两种格式。"""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(content)


def _usage_metadata(usage: Any) -> dict[str, Any] | None:
    """把方舟的 `CompletionUsage` 映射成 LangChain 的 `usage_metadata`。

    LangChain 的 openai / anthropic 集成统一用 `input_tokens` / `output_tokens` /
    `total_tokens` 这套键名，方舟侧转成同一种形状，上游（`stream_reply`）就只需认一种。
    字段缺失时返回 `None`——「没给用量」与「用量为零」是两回事，前者不该被记成一次零消耗。
    """
    if usage is None:
        return None
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if prompt_tokens is None and completion_tokens is None:
        return None
    input_tokens = int(prompt_tokens or 0)
    output_tokens = int(completion_tokens or 0)
    total_tokens = getattr(usage, "total_tokens", None)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(total_tokens) if total_tokens is not None else input_tokens + output_tokens,
    }


def _message_to_ark(message: BaseMessage) -> dict[str, Any]:
    """把 LangChain 消息映射成方舟请求体里的消息 dict。

    除了正文与角色，还序列化两类「工具」字段：
    - `AIMessage.tool_calls` → `tool_calls`（`[{id, type, function:{name, arguments}}]`，
      `arguments` 为 JSON 字符串）——历史里模型上一轮的工具调用；
    - `ToolMessage.tool_call_id` → `tool_call_id`——工具结果回灌时，模型据此对回自己的调用。
    """
    role = _role_of(message)
    out: dict[str, Any] = {"role": role, "content": _content_of(message)}

    tool_calls = getattr(message, "tool_calls", None)
    if role == "assistant" and tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.get("id"),
                "type": "function",
                "function": {
                    "name": tc.get("name"),
                    "arguments": json.dumps(tc.get("args") or {}, ensure_ascii=False),
                },
            }
            for tc in tool_calls
        ]

    if role == "tool":
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id:
            out["tool_call_id"] = tool_call_id

    return out


def _tool_call_chunks(raw: Any) -> list[ToolCallChunk]:
    """把方舟 `delta.tool_calls` 映射成 LangChain 的 `ToolCallChunk` 列表。

    `args` 是**分片 JSON 字符串**（不是 dict），交由 LangChain 的 `default_tool_chunk_parser`
    在合并后拼成完整 `tool_calls`——**不要**在这里自己拼 JSON。
    """
    if not raw:
        return []
    chunks: list[ToolCallChunk] = []
    for tc in raw:
        fn = getattr(tc, "function", None)
        chunks.append(
            ToolCallChunk(
                name=getattr(fn, "name", None) if fn is not None else None,
                args=getattr(fn, "arguments", None) if fn is not None else None,
                id=getattr(tc, "id", None),
                index=getattr(tc, "index", 0),
            )
        )
    return chunks


class VolcanoArkChatModel(BaseChatModel):
    """基于官方 arkruntime 的 LangChain 异步流式聊天模型。"""

    api_key: str
    model: str
    base_url: str | None = None
    temperature: float = 0.7
    max_tokens: int | None = None
    thinking_enabled: bool = False
    timeout: float | None = None

    model_config = ConfigDict(extra="ignore")

    @property
    def _llm_type(self) -> str:
        return "volcano_ark"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """同步生成（抽象方法，必须实现）。业务只走 `astream`，此路径仅兜底。

        用独立事件循环收集 `_astream` 的全部分块并合并，避免与调用方的运行中循环冲突。
        """
        merged: AIMessageChunk | None = None

        async def _collect() -> None:
            nonlocal merged
            async for chunk in self._astream(
                messages, stop=stop, run_manager=run_manager, **kwargs
            ):
                merged = chunk.message if merged is None else merged + chunk.message

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_collect())
        finally:
            loop.close()

        message = merged.to_message() if merged is not None else AIMessage(content="")
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(
        self,
        tools: list[dict[str, Any]],
        *,
        tool_choice: str | None = None,
        parallel_tool_calls: bool | None = None,
        **kwargs: Any,
    ) -> Any:
        """把工具绑定到模型（覆写基类占位实现）。

        方舟的工具形状本就是 OpenAI 兼容的 `[{"type":"function","function":{...}}]`，
        直接透传即可（无需 `convert_to_openai_tool`）。绑定的 kwargs 会经
        `BaseChatModel.astream` 透传进 `_astream`（见 chat_models.py）。
        """
        return self.bind(
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            **kwargs,
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """异步流式产出模型回复，每块为 `ChatGenerationChunk(message=AIMessageChunk)`。

        `kwargs` 里带 `bind_tools` 塞进来的 `tools` / `tool_choice` /
        `parallel_tool_calls`，有 `tools` 时才下发。
        """
        tools = kwargs.get("tools")
        tool_choice = kwargs.get("tool_choice")
        parallel_tool_calls = kwargs.get("parallel_tool_calls")

        client_kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        # 不配超时时不传这个参数，沿用 SDK 自己的默认值
        if self.timeout is not None:
            client_kwargs["timeout"] = self.timeout
        client = AsyncArk(**client_kwargs)
        try:
            request: dict[str, Any] = {
                "model": self.model,
                "messages": [_message_to_ark(message) for message in messages],
                "stream": True,
                "temperature": self.temperature,
                # 索取用量：不加这一项时，方舟在流式模式下不回吐任何 token 计数
                # （`include_usage` 的定义是「把用量放进最后一个流式分块」）
                "stream_options": {"include_usage": True},
            }
            # 显式指定单次回复的 token 上限，避免长文档被默认上限截断
            if self.max_tokens is not None:
                request["max_tokens"] = self.max_tokens
            # 仅对自带思考的方舟模型下发；默认关闭，直接回答。
            if not self.thinking_enabled:
                request["thinking"] = {"type": "disabled"}
            # 工具调用：只有绑定过工具才下发
            if tools:
                request["tools"] = tools
            if tool_choice is not None:
                request["tool_choice"] = tool_choice
            if parallel_tool_calls is not None:
                request["parallel_tool_calls"] = parallel_tool_calls

            stream = await client.chat.completions.create(**request)
            # 跨块记住结束原因：开启用量后，**携带用量的那一帧 `choices` 为空**，
            # 它自己没有 finish_reason，必须在之前的正文帧上记下来再带到最后，
            # 否则上游的截断检测（读最后一块的 finish_reason）会永远判不出截断。
            last_finish_reason = ""
            # 真末块引用：流结束后要给它打 `chunk_position="last"` 标记（见下方说明）。
            last_message: AIMessageChunk | None = None
            async for chunk in stream:
                # 先取用量、再看 choices：这是与旧实现的**唯一实质差异**。旧写法
                # `if not chunk.choices: continue` 会把用量帧整帧丢掉，表现为
                # 「开了 include_usage 却永远拿不到 usage」。
                usage_metadata = _usage_metadata(getattr(chunk, "usage", None))
                choices = getattr(chunk, "choices", None) or []

                message: AIMessageChunk | None = None
                if choices:
                    choice = choices[0]
                    delta = getattr(choice, "delta", None)
                    text = getattr(delta, "content", None) or ""
                    tool_call_chunks = _tool_call_chunks(getattr(delta, "tool_calls", None))
                    finish_reason = getattr(choice, "finish_reason", None)
                    if finish_reason:
                        last_finish_reason = str(finish_reason)
                    if text or tool_call_chunks or last_finish_reason:
                        message = AIMessageChunk(
                            content=text,
                            tool_call_chunks=tool_call_chunks,
                            response_metadata=(
                                {"finish_reason": last_finish_reason}
                                if last_finish_reason
                                else {}
                            ),
                            usage_metadata=usage_metadata,
                        )
                elif usage_metadata is not None:
                    # 用量帧：没有正文，但要把结束原因一并透出，让最后一块自洽
                    message = AIMessageChunk(
                        content="",
                        response_metadata=(
                            {"finish_reason": last_finish_reason}
                            if last_finish_reason
                            else {}
                        ),
                        usage_metadata=usage_metadata,
                    )
                if message is not None:
                    last_message = message
                    yield ChatGenerationChunk(message=message)

            # 标记真末块（LangChain 1.x 约定）：基类 `astream` 在流结束后若发现最后一块
            # 没带 `chunk_position="last"`，会**再补一个空尾巴块**。那个尾巴块既没有
            # `usage_metadata` 也没有 `finish_reason`，而 `stream_reply` 的计量与截断检测
            # 都读「最后一块」——尾巴块一出，用量被记成零、截断被静默吞掉。
            # 把真末块标上 "last" 即可抑制尾巴块。注意必须在生成器收尾**之前**改：
            # 基类是等 `_astream` 耗尽后才检查这个标记，此时改对象它看得到（同一引用）。
            if last_message is not None:
                last_message.chunk_position = "last"
        finally:
            await client.close()
