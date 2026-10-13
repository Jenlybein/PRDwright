"""对话提示词的归属与口径锁定。

澄清对话的**系统人设**与**首轮开场白**全部归后端所有，且被打包在同一个
`ConversationProfile` 里（`services/prompts.py`）。

在此之前它们是分居两处的：人设在前端 `useClarificationChat.ts`，开场白在后端
`conversation_service.py`。人设写「1-3 个问题」、开场白写「3-5 个问题」，而开场白是
对话里更靠后、更具体的一条指令，实际把上限盖成了 5，表现为「首轮复述完理解后紧接
一大串问题」。这里把归属（前端零提示词）与口径（同一档案内一致）一起锁死。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from backend.api.schemas import ConversationContinueRequest, ConversationCreateRequest
from backend.services.conversation_service import ConversationService
from backend.services.prompts import (
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


def test_default_profile_is_registered() -> None:
    """默认档案必须登记在册——对话接口不接受档案参数，默认值就是唯一入口。"""
    assert DEFAULT_CONVERSATION_PROFILE in CONVERSATION_PROFILES


def test_persona_and_opening_live_in_the_same_profile() -> None:
    """人设与开场白必须同属一个档案对象，口径才不可能再漂移。"""
    profile = resolve_profile(DEFAULT_CONVERSATION_PROFILE)
    assert profile.key == DEFAULT_CONVERSATION_PROFILE
    assert profile.system_prompt.strip()
    assert profile.opening.strip()


def test_question_cap_is_consistent_within_profile() -> None:
    """同一档案内，人设与开场白都要写 1-2 个，且都不残留旧口径。"""
    profile = resolve_profile(DEFAULT_CONVERSATION_PROFILE)
    assert "每轮最多 1-2 个问题" in profile.system_prompt
    assert "1-2 个" in profile.opening
    for text in (profile.system_prompt, profile.opening):
        assert "1-3 个" not in text
        assert "3-5 个" not in text


def test_opening_forbids_dumping_the_whole_question_list() -> None:
    """开场白要明确「多轮逐步澄清」，禁止把待澄清清单一次铺完。"""
    opening = resolve_profile(DEFAULT_CONVERSATION_PROFILE).opening
    assert "多轮" in opening
    assert "不要把所有待澄清" in opening


def test_opening_is_a_fillable_template() -> None:
    """开场白是模板：必须留出 `{requirements}` 占位符供服务层填充。"""
    assert "{requirements}" in resolve_profile(DEFAULT_CONVERSATION_PROFILE).opening


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
