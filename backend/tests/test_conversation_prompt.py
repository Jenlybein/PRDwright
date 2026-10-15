"""对话提示词的归属与口径锁定。

澄清对话的**系统人设**与**首轮开场白**全部归后端所有，且被打包在同一个
`ConversationProfile` 里（`services/prompts.py`）。

在此之前它们是分居两处的：人设在前端 `useClarificationChat.ts`，开场白在后端
`conversation_service.py`。人设写「1-3 个问题」、开场白写「3-5 个问题」，而开场白是
对话里更靠后、更具体的一条指令，实际把上限盖成了 5，表现为「首轮复述完理解后紧接
一大串问题」。

现在口径由**共享常量**（`CLARIFICATION_QUESTION_RULE`）承载：新增一个澄清类档案只是
引用它，不可能再各抄一个数字。本文件把归属（前端零提示词）、口径（同一档案内一致）
与模板契约（占位符声明 ↔ 模板）三件事一起锁死，且**遍历全部档案**。
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from pathlib import Path
from typing import AsyncIterator

import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.api.schemas import ConversationContinueRequest, ConversationCreateRequest
from backend.core.contracts import StreamEvent
from backend.services.conversation_service import ConversationService
from backend.services.prompts import (
    CLARIFICATION_QUESTION_CAP,
    CLARIFICATION_QUESTION_RULE,
    CONVERSATION_PROFILES,
    DEFAULT_CONVERSATION_PROFILE,
    resolve_profile,
)

# 仓库根 → 前端源码目录（守卫用：前端不得再持有任何提示词正文）
_FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"

# 提示词正文的特征片段：任一处出现在前端源码里，都说明提示词又「漏」回了前端
_PROMPT_MARKERS = (
    "资深产品经理",
    "每轮最多 1-2 个问题",
    "绝不生成 PRD",
    "信息已经足够，可以点击生成 PRD",
)

# 已知的过期口径：它们只该出现在本文件的断言里
_STALE_CAPS = ("1-3 个", "3-5 个")


def test_default_profile_is_registered() -> None:
    """默认档案必须登记在册——对话接口留空档案时，默认值就是唯一入口。"""
    assert DEFAULT_CONVERSATION_PROFILE in CONVERSATION_PROFILES


def test_persona_and_opening_live_in_the_same_profile() -> None:
    """每个档案的人设与开场白都必须同属一个对象，口径才不可能再漂移。"""
    for key, profile in CONVERSATION_PROFILES.items():
        assert profile.key == key
        assert profile.system_prompt.strip()
        assert profile.opening.strip()
        assert profile.label.strip()


def test_question_cap_comes_from_the_shared_rule() -> None:
    """提问上限由共享常量承载：每个档案都引用它，不是各写一个数字。

    这条是「口径不可漂移」的机制性保证——只要有人试图在某个档案里手写一个上限，
    它就不再是共享规则，这里的断言会立刻失败。
    """
    assert CLARIFICATION_QUESTION_RULE in CONVERSATION_PROFILES["clarification"].system_prompt
    for profile in CONVERSATION_PROFILES.values():
        assert CLARIFICATION_QUESTION_RULE in profile.system_prompt, profile.key
        assert f"每轮最多 {CLARIFICATION_QUESTION_CAP} 个问题" in profile.system_prompt


def test_question_cap_is_consistent_across_all_profiles() -> None:
    """每个档案内、以及档案之间，上限口径都一致，且不残留旧数字。"""
    for key, profile in CONVERSATION_PROFILES.items():
        # 开场白（对话里更靠后、更具体的那条指令）同样受这个上限约束
        assert f"{CLARIFICATION_QUESTION_CAP} 个" in profile.opening, key
        for text in (profile.system_prompt, profile.opening):
            for stale in _STALE_CAPS:
                assert stale not in text, f"{key} 残留旧口径：{stale}"


def test_opening_forbids_dumping_the_whole_question_list() -> None:
    """开场白要明确「多轮逐步澄清」，禁止把待澄清清单一次铺完。"""
    for profile in CONVERSATION_PROFILES.values():
        assert "多轮" in profile.opening
        assert "不要把所有" in profile.opening


def test_opening_fields_match_the_placeholders() -> None:
    """开场白的占位符声明与模板必须**一一对应**。

    多声明一个会在渲染时 `KeyError`（一条 500），少声明一个则拿不到值、静默填空串——
    两个方向都要挡住，因此这里做的是集合相等而不是「包含」。
    """
    for key, profile in CONVERSATION_PROFILES.items():
        placeholders = set(re.findall(r"\{(\w+)\}", profile.opening))
        assert placeholders == set(profile.opening_fields), key
        assert placeholders, f"{key} 的开场白没有任何占位符"


def test_profile_is_immutable() -> None:
    """档案是冻结对象：人设与开场白不能被就地改写，否则「同口径」就失去保证。"""
    profile = resolve_profile(DEFAULT_CONVERSATION_PROFILE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.system_prompt = "就地改一下"  # type: ignore[misc]


def test_unknown_profile_key_raises() -> None:
    """写错档案名要立刻报错，不能静默退回默认人设。"""
    with pytest.raises(KeyError):
        resolve_profile("does-not-exist")
    with pytest.raises(KeyError):
        ConversationService(profile_key="does-not-exist")


def test_conversation_requests_expose_no_prompt_field() -> None:
    """接口契约里不得再有提示词字段：前端只能提交内容，不能自带提示词。"""
    for model in (ConversationCreateRequest, ConversationContinueRequest):
        assert "system_prompt_override" not in model.model_fields
        assert "profile" not in model.model_fields


class _RecordingLLM:
    """记录收到的消息的假 LLM：用来断言「到底把什么填进了提示词」。

    只实现 `astream`（满足 `StreamingChatModel` 协议），吐一个字就够了——本组测试关心的是
    输入侧，不是输出侧。
    """

    def __init__(self) -> None:
        self.calls: list[list[BaseMessage]] = []

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        self.calls.append(list(messages))
        yield AIMessageChunk(content="收到")


def _drain(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    """同步跑完一条事件流并收集全部事件。"""

    async def run() -> list[StreamEvent]:
        return [event async for event in events]

    return asyncio.run(run())


def test_doc_clarification_opening_carries_document_and_pending_items() -> None:
    """「文档澄清」的第一条 user 消息里，文档正文与待确认清单都被真正填了进去。

    占位符声明与模板一一对应只保证了「不会 KeyError」，这条保证「填的是对的东西」。
    """
    llm = _RecordingLLM()
    service = ConversationService(llm=llm)

    _drain(
        service.start_conversation_stream(
            form_data={
                "document": "# 演示产品 PRD\n\n正文一段。",
                "pending_items": "1. 分页上限取多少？（涉及章节：4.3）",
            },
            profile_key="doc-clarification",
        )
    )

    first = str(llm.calls[0][-1].content)
    assert "# 演示产品 PRD" in first
    assert "1. 分页上限取多少？" in first
    # 用的是文档澄清的人设，不是需求澄清
    assert "已经写好" in str(llm.calls[0][0].content)


def test_continue_without_record_keeps_the_named_profile() -> None:
    """无状态接续（服务重启 / 会话过期）时靠 `profile_key` 保住场景。

    没有它就只能回退默认档案，于是「文档澄清」会在中途悄悄变回「需求澄清」——
    用户会突然被问「要不要生成 PRD」，而没有任何报错。
    """
    llm = _RecordingLLM()
    service = ConversationService(llm=llm)

    _drain(
        service.continue_conversation_stream(
            user_message="分页上限就定 50。",
            history=[{"role": "assistant", "content": "先问最关键的一个。"}],
            profile_key="doc-clarification",
        )
    )

    assert "已经写好" in str(llm.calls[0][0].content)


def test_unknown_profile_key_is_rejected_not_silently_ignored() -> None:
    """未注册的档案名当场拒绝，绝不静默回退默认人设。"""
    from backend.core.errors import AppError, ErrorCode
    from backend.services.conversation_service import check_profile_key

    # 留空是「没点名」，取默认档案，不报错
    check_profile_key("")

    with pytest.raises(AppError) as excinfo:
        check_profile_key("does-not-exist")
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_http_rejects_unknown_profile_key_with_400(client) -> None:
    """HTTP 面：未知档案名是一个真正的 400，而不是 200 流里的一条 error 事件。

    校验必须发生在**构造 SSE 响应之前**——进了流之后状态码恒为 200，前端拿不到 400，
    只能靠错误码猜，那就等于把「你点名了一个不存在的场景」降级成「生成失败了」。
    """
    from conftest import register

    register(client)

    created = client.post(
        "/api/conversation/start-stream",
        json={"answers": {}, "profile_key": "does-not-exist"},
    )
    assert created.status_code == 400

    continued = client.post(
        "/api/conversation/continue-stream",
        json={"content": "在吗", "profile_key": "does-not-exist"},
    )
    assert continued.status_code == 400


def test_frontend_holds_no_prompt_text() -> None:
    """前端源码里不得出现提示词正文。

    测试文件（`__tests__`）除外——它需要引用这些片段来做「不存在」的断言。
    """
    if not _FRONTEND_SRC.exists():  # 单独跑 backend 时前端可能不在
        pytest.skip("前端源码目录不存在，跳过跨端守卫检查")

    offenders: list[str] = []
    for path in _FRONTEND_SRC.rglob("*"):
        if path.suffix not in {".ts", ".vue"} or "__tests__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.relative_to(_FRONTEND_SRC)} → {marker}"
            for marker in _PROMPT_MARKERS
            if marker in text
        )

    assert not offenders, "提示词又回到了前端：" + "；".join(offenders)
