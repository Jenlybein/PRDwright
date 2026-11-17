"""分批续写（票 #93）的契约测试。

撞上模型输出上限时**自动接着写**，直到写完；某批失败在批次内重试，用尽则整篇失败但
保留已生成部分。三份产物（PRD / 接口文档 / 提示词套件）共用同一条机制。

## 判据是结束原因，不是字数

「要不要续写」只看模型返回的结束原因（`finish_reason` / `stop_reason` ∈
{`length`, `max_tokens`}），不猜字数、不看输出是否「看起来没写完」——阈值迟早会在
某份偏短的文档上误判，把一次正常生成拦腰截成两批（或该续写时不续写）。

## 接缝：不重复、不漏字

续写批次的开头可能与上一批结尾**逐字重合**（模型重新起个头）。机制上先扣住批次开头，
与已写正文的尾部做最长后缀匹配，只丢掉那段**已经紧挨在正文末尾、逐字存在**的重复；
其余一个字节都不动。因此「不漏字」是构造性的：丢掉的字符在紧邻的前文里原样存在。

用例外置的接缝只有两条：`stream_reply.iter_continuation_events`（续写循环本身）与
三个生成入口（PRD / 接口文档 / 提示词），不测内部类。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage

from backend.api.schemas import StreamEvent
from backend.core.config import get_settings
from backend.services.document_service import DocumentService
from conftest import register, users
from backend.services.stream_reply import (
    MAX_BATCH_ATTEMPTS,
    MAX_CONTINUATION_BATCHES,
    iter_continuation_events,
)

# 结构化摘要（与 test_prd_whole_stream 同形：生成路径只要求它是可渲染的字典）
_SUMMARY: dict[str, Any] = {
    "product_name": "问卷星Plus",
    "product_goal": "让产品团队自助收集需求问卷",
    "target_users": ["产品经理"],
    "platform": "Web",
    "mvp_features": [{"name": "问卷编辑", "module": "问卷", "description": "拖拽式编辑"}],
    "v2_features": [],
    "ui_pages": [],
    "technical_constraints": ["后端用 Python"],
}

_PRD_BODY = "# 问卷星Plus 产品需求文档（PRD）\n\n## 1. 产品概述\n\n正文开头。"


class ScriptedCallLLM:
    """按调用次序执行一份「调用脚本」的假模型。

    每次调用取脚本里的下一条，条目有三种形态：

    - `(文本, finish_reason)`：吐一段文本，**末块**带结束原因（续写判据靠它）；
    - `异常实例`：一进流就抛（模拟连接层失败，`astream` 之前就挂）；
    - `(文本, finish_reason, 异常实例)`：先把文本吐完，再抛（模拟「写到一半断流」）。

    文本一律切成两片吐出，覆盖「增量拼接」那条路径。
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        # 每次调用收到的完整消息（供「续写提示词里到底带了什么」这类断言使用）
        self.calls: list[list[BaseMessage]] = []

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        index = len(self.calls)
        self.calls.append(list(messages))
        step = self._script[index] if index < len(self._script) else ("", "stop")

        if isinstance(step, BaseException):
            raise step

        if len(step) == 3:
            text, finish_reason, error = step
        else:
            text, finish_reason = step
            error = None

        pieces = [text[: len(text) // 2], text[len(text) // 2 :]]
        for position, piece in enumerate(pieces):
            if not piece:
                continue
            last = position == len(pieces) - 1
            yield AIMessageChunk(
                content=piece,
                response_metadata={"finish_reason": finish_reason} if last else {},
            )
        if error is not None:
            raise error


class UpstreamConnectionError(ConnectionError):
    """可重试的传输层失败（类名带 `Connection`，与 `_is_retryable` 的白名单对齐）。

    ⚠️ 不能用 `TimeoutError` 的子类冒充：`stream_reply._total_deadline` 会把墙钟超时
    统一转成 `CallTimeoutError`（`AppError`，按设计**不重试**——慢到顶的调用重来一样慢）。
    要测批次级重试，就得用一个真正的传输层异常。
    """


@pytest.fixture(autouse=True)
def _single_attempt_per_call(monkeypatch):
    """关掉「单次调用内的自愈重试」，本文件测的是**批次级**重试。

    两者不是一回事：调用内的自愈只管「首字之前」的抖动（`stream_reply._astream_chunks`
    的既有口径），批次级重试管的是「这一批写到一半断了」——把两层混在一起测，
    就分不清是哪一层在起作用了。
    """
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_RETRY_DELAY", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _drain(agen: Any) -> list[StreamEvent]:
    async def run() -> list[StreamEvent]:
        return [event async for event in agen]

    return asyncio.run(run())


def _chunks(events: list[StreamEvent]) -> str:
    return "".join(event.content for event in events if event.type == "chunk")


def _done_reply(events: list[StreamEvent]) -> str:
    return str(next(event for event in events if event.type == "done").data["reply"])


def _prd_events(llm: Any) -> list[StreamEvent]:
    return _drain(
        DocumentService(llm=llm).generate_prd_from_summary_stream(
            summary=_SUMMARY, review_enabled=False
        )
    )


def _api_docs_events(llm: Any) -> list[StreamEvent]:
    return _drain(
        DocumentService(llm=llm).generate_api_docs_stream(
            prd_content=_PRD_BODY, review_enabled=False
        )
    )


def _prompts_events(llm: Any) -> list[StreamEvent]:
    return _drain(
        DocumentService(llm=llm).generate_prompts_stream(
            prd_content=_PRD_BODY, review_enabled=False
        )
    )


# ---------------------------------------------------------------------------
# AC1：撞上限后自动续写，最终得到完整正文
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["length", "max_tokens"])
def test_truncated_batch_is_continued_until_the_document_is_finished(reason: str) -> None:
    """结束原因表明「被截断」→ 自动补一批，直到模型正常收尾。"""
    llm = ScriptedCallLLM([("第一段写到一半", reason), ("，第二段写完。", "stop")])

    events = _prd_events(llm)

    assert not [event for event in events if event.type == "error"]
    assert _done_reply(events) == "第一段写到一半，第二段写完。"
    assert _chunks(events).strip() == _done_reply(events)
    assert len(llm.calls) == 2, "被截断就该补第二批"


def test_a_batch_that_ended_normally_is_not_continued() -> None:
    """正常收尾（`stop`）不续写：判据是结束原因，不是「看起来短」。"""
    llm = ScriptedCallLLM([("整篇就这一句话。", "stop")])

    events = _prd_events(llm)

    assert len(llm.calls) == 1
    assert _done_reply(events) == "整篇就这一句话。"
    assert not [event for event in events if event.data.get("continuation")]


# ---------------------------------------------------------------------------
# AC2：批次之间不重复、不漏字
# ---------------------------------------------------------------------------


def test_continuation_does_not_repeat_the_previous_ending() -> None:
    """模型重起头把上一批结尾又说了一遍 → 用户可见正文里只出现一次。"""
    llm = ScriptedCallLLM([("第一段写到一半。", "length"), ("第一段写到一半。第二段写完。", "stop")])

    events = _prd_events(llm)

    assert _done_reply(events) == "第一段写到一半。第二段写完。"
    assert _chunks(events).count("第一段写到一半。") == 1
    assert _chunks(events).strip() == _done_reply(events)


def test_continuation_repeats_nothing_when_the_model_keeps_writing_forward() -> None:
    """续写不重合时**一个字节都不许动**：漏字不可恢复，重复只是观感。"""
    llm = ScriptedCallLLM([("第一段写到一半", "length"), ("，第二段写完。", "stop")])

    events = _prd_events(llm)

    assert _done_reply(events) == "第一段写到一半，第二段写完。"


def test_a_shared_word_across_the_seam_is_not_eaten_twice() -> None:
    """接缝上「续写从上一批结尾那几个字起头」时，重合部分只保留一份。"""
    llm = ScriptedCallLLM(
        [("每个用户只能绑定一个账号", "length"), ("账号绑定后不可更换。", "stop")]
    )

    events = _prd_events(llm)

    assert _done_reply(events) == "每个用户只能绑定一个账号绑定后不可更换。"
    assert "账号账号" not in _done_reply(events)


def test_the_continuation_request_carries_the_tail_and_forbids_repeating() -> None:
    """续写请求带的是**尾部摘录**（不是全文）＋一句明确的「接着写、不要重复」。"""
    llm = ScriptedCallLLM([("第一段写到一半。", "length"), ("第二段写完。", "stop")])

    _prd_events(llm)

    continuation = llm.calls[1]
    # 上一批的结尾以 assistant 回合交回去：模型据此「接着写」，而不是重写
    assert any(str(message.content).endswith("第一段写到一半。") for message in continuation)
    instruction = str(continuation[-1].content)
    assert isinstance(continuation[-1], HumanMessage)
    assert "不要重复" in instruction
    assert "接着写" in instruction


def test_the_continuation_request_does_not_carry_the_whole_document() -> None:
    """只带尾部：每批都塞全文会把续写请求自己顶到上下文窗口上。"""
    long_tail = "前半段内容重复出现。" * 400  # 约 4000 字，远超尾部窗口
    llm = ScriptedCallLLM([(long_tail, "length"), ("收尾。", "stop")])

    _prd_events(llm)

    continuation = llm.calls[1]
    excerpt = str(continuation[-2].content)
    assert long_tail not in excerpt, "续写请求里出现了整篇正文"
    assert "前半段内容重复出现。" in excerpt, "尾部摘录必须包含上一批的结尾"


# ---------------------------------------------------------------------------
# AC3：批次内重试；用尽则整篇失败但保留已生成部分
# ---------------------------------------------------------------------------


def test_a_failed_batch_is_retried_within_the_batch() -> None:
    """续写批次断流 → 同一个批次内重试，正文接着长（不重写开头、不丢已写部分）。"""
    llm = ScriptedCallLLM(
        [
            ("第一段写到一半", "length"),
            ("，第二段", "length", UpstreamConnectionError("上游超时")),
            ("第二段写完。", "stop"),
        ]
    )

    events = _prd_events(llm)

    assert not [event for event in events if event.type == "error"]
    assert len(llm.calls) == 3, "断流的那一批应当在批次内重试一次"
    # 失败那一批已经流出去的「，第二段」留在正文里，重试只补后面那半截
    assert _done_reply(events) == "第一段写到一半，第二段写完。"


def test_a_batch_that_keeps_failing_fails_the_document_and_keeps_the_written_part() -> None:
    """重试用尽 → 整篇失败（`error`，没有 `done`），**已生成的正文一个字都不撤**。"""
    llm = ScriptedCallLLM(
        [("第一段写到一半", "length")]
        + [UpstreamConnectionError("上游超时")] * MAX_BATCH_ATTEMPTS
    )

    events = _prd_events(llm)

    assert [event.type for event in events][-1] == "error"
    assert not [event for event in events if event.type == "done"]
    # 已生成部分还在流里（收尾按「部分完成」落一版，是 #85 的能力，本票只负责不撤）
    assert _chunks(events).strip() == "第一段写到一半"
    # 重试次数有上限：首批 1 次 + 批次内最多 MAX_BATCH_ATTEMPTS 次
    assert len(llm.calls) <= 1 + MAX_BATCH_ATTEMPTS


def test_continuation_is_bounded_so_a_runaway_model_cannot_loop_forever() -> None:
    """永远报「被截断」的模型：写到批数上限就收成失败，不无限续写。"""
    llm = ScriptedCallLLM([("还在写。", "length")] * (MAX_CONTINUATION_BATCHES + 3))

    events = _prd_events(llm)

    assert len(llm.calls) == MAX_CONTINUATION_BATCHES
    assert [event.type for event in events][-1] == "error"
    assert "截断" in events[-1].content


# ---------------------------------------------------------------------------
# AC4：续写过程对外可见，进度持续推进（不回到起点）
# ---------------------------------------------------------------------------


def test_continuation_is_visible_and_never_rewinds_the_stage() -> None:
    """每续一批发一条 `stage(writing)` + 批次序号；阶段**只前进不回到大纲**。"""
    llm = ScriptedCallLLM(
        [("第一批。", "length"), ("第二批。", "length"), ("第三批。", "stop")]
    )

    events = _prd_events(llm)

    stages = [str(event.data.get("stage")) for event in events if event.type == "stage"]
    assert stages[0] == "outlining"
    assert stages.count("writing") == 3, "一批一条 writing，续写才不会看起来停住"
    # 续写期间不再回过头去发大纲：进度条不许回到起点
    assert "outlining" not in stages[1:]

    continuations = [
        event.data.get("continuation") for event in events if event.data.get("continuation")
    ]
    assert continuations == [2, 3], "第二条起每批带批次序号（第几批正文）"
    assert _done_reply(events) == "第一批。第二批。第三批。"


# ---------------------------------------------------------------------------
# AC5：三份产物行为一致
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "run"),
    [
        ("prd", _prd_events),
        ("api_docs", _api_docs_events),
        ("prompts", _prompts_events),
    ],
)
def test_all_three_products_continue_the_same_way(name: str, run: Any) -> None:
    """三份产物共用同一条续写机制：判据、接缝、进度事件逐项一致。"""
    llm = ScriptedCallLLM([("上半篇。", "length"), ("上半篇。下半篇。", "stop")])

    events = run(llm)

    assert not [event for event in events if event.type == "error"], name
    assert len(llm.calls) == 2, name
    assert _done_reply(events) == "上半篇。下半篇。", name
    assert "continuation" in {
        key for event in events if event.type == "stage" for key in event.data
    }, name


# ---------------------------------------------------------------------------
# AC3 端到端：批次重试用尽 → 任务失败 + 已生成部分落成一版「部分完成」
# ---------------------------------------------------------------------------


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """从 SSE 文本里取出各事件的 JSON 体（忽略 `[DONE]` 结束标记）。"""
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload and payload != "[DONE]":
                events.append(json.loads(payload))
    return events


class _PacedService:
    """把文档服务包一层：**第一个事件之前**先等一会，让订阅者来得及挂上来。"""

    def __init__(self, inner: DocumentService) -> None:
        self._inner = inner

    async def generate_prd_from_summary_stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        await asyncio.sleep(0.1)
        async for event in self._inner.generate_prd_from_summary_stream(**kwargs):
            yield event


def test_exhausted_batches_land_the_written_part_as_a_partial_version(client, monkeypatch) -> None:
    """批次重试用尽 → 任务失败，但已写出的部分落成一版带标记的正文（#85 的能力，本票接上）。

    接缝是任务通道（`POST /api/jobs` → `GET /{id}/stream`）：只替换 LLM，
    鉴权 / 路由 / SSE 分帧 / 收尾落库全走真实实现。这一条是「失败但保留已生成部分」
    唯一的端到端证据——服务层的用例只能证明字流出来了，落版本得由收尾证明。
    """
    from backend.services import document_service, document_store

    llm = ScriptedCallLLM(
        [("第一段写到一半。", "length")]
        + [UpstreamConnectionError("上游中断")] * MAX_BATCH_ATTEMPTS
    )
    monkeypatch.setattr(
        document_service, "get_service", lambda: _PacedService(DocumentService(llm=llm))
    )

    assert register(client).status_code == 200
    uid = int(users.get_by_username("alice")["id"])  # type: ignore[index]
    created = client.post(
        "/api/solutions",
        json={
            "title": "分批续写用例",
            "mode": "structured",
            "stage": "review-prd",
            "state": {"entry_mode": "prd-quick", "quick_requirements": "做一个演示产品"},
        },
    )
    assert created.status_code == 201, created.text
    solution_id = int(created.json()["id"])
    job = client.post("/api/jobs", json={"kind": "prd", "solution_id": solution_id})
    assert job.status_code == 201, job.text
    stream = client.get(f"/api/jobs/{job.json()['job_id']}/stream")
    assert stream.status_code == 200

    events = _parse_sse(stream.text)
    types = [event.get("type") for event in events]
    assert "error" in types, f"重试用尽必须报失败：{types}"
    assert "done" not in types, "失败不许发 done"

    versions = document_store.list_versions(uid, solution_id, "prd")
    assert versions, "已生成的部分必须落一版，不能什么都不留"
    assert versions[0]["partial"] is True
    detail = document_store.get_version(uid, solution_id, "prd", versions[0]["version_id"])
    assert detail is not None
    assert "第一段写到一半。" in detail["content"]


# ---------------------------------------------------------------------------
# 单元接缝：续写循环本身（不经过三份产物的提示词）
# ---------------------------------------------------------------------------
_BASE_MESSAGES: list[BaseMessage] = [
    HumanMessage(content="写一份说明"),
]


def _continue_messages(tail: str) -> list[BaseMessage]:
    return [HumanMessage(content=f"接着写，不要重复：{tail}")]


def test_the_loop_reports_the_final_reply_in_done() -> None:
    """`done.reply` 是**拼接后**的完整正文，与流出去的增量逐字一致。"""
    llm = ScriptedCallLLM([("甲", "length"), ("乙", "stop")])

    events = _drain(
        iter_continuation_events(
            llm,
            _BASE_MESSAGES,
            continuation_messages=_continue_messages,
            extra_data={"doc_type": "prd"},
        )
    )

    assert [event.type for event in events] == ["chunk", "stage", "chunk", "done"]
    assert events[1].data.get("continuation") == 2
    assert events[-1].data == {"doc_type": "prd", "reply": "甲乙"}
