"""复述判据：撰写调用的输出若是「把参考材料原样复述」，必须作废而不是落库。

线上真出现过一次（2026-11-18，方案 1 的第 2 版 PRD）：PRD 的撰写调用把上一轮澄清
回复整段当正文吐了出来——开头是「已经拿到你这份结构化需求……」。随后审核拿 PRD 的
标准给这段对话挑毛病、改写再改一遍，最后当成一版 PRD 写进版本链。

这里钉住三件事：
1. `echoes_reference` 的判据本身（能证伪，不靠词表）；
2. 疑似复述时**不落 `done`**（没有 `done.reply`，失败收尾就没有「可落」的正文）；
3. 重试一次后仍复述 → 以 `MODEL_OUTPUT_NOT_DOCUMENT` 失败收尾，且第二次调用带上了
   硬提示（不是原地重跑一遍原提示词）。
"""

from typing import Any, AsyncIterator

import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.api.schemas import StreamEvent
from backend.services.document_service import DocumentService
from backend.services.stream_guard import echoes_reference

# 与线上那次一模一样的开头（照抄对话记录）。
_ECHOED = "已经拿到你这份结构化需求，背景不用再重复交代。下面是理解确认与这一轮的问题。"
_CONVERSATION = f"**AI**：{_ECHOED}\n\n## 一、我对产品的理解\n\n- **它是什么**：一个需求工作台。"

# 一份正常的 PRD 正文。
_PRD_BODY = "# 问卷星Plus 产品需求文档（PRD）\n\n## 1. 产品概述\n\n正文开头。"

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


class _ScriptedLLM:
    """按调用次序取脚本的假模型（每轮把文本切成两片，覆盖增量拼接）。"""

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        index = len(self.calls)
        self.calls.append(list(messages))
        text = self._script[index] if index < len(self._script) else ""
        middle = max(1, len(text) // 2)
        yield AIMessageChunk(content=text[:middle])
        yield AIMessageChunk(content=text[middle:])


def _drain(service: DocumentService, history: list[dict[str, str]]) -> list[StreamEvent]:
    events: list[StreamEvent] = []

    async def run() -> None:
        async for event in service.generate_prd_from_summary_stream(
            summary=_SUMMARY, history=history, review_enabled=False
        ):
            events.append(event)

    import asyncio

    asyncio.run(run())
    return events


def test_echo_judgement_is_not_vacuous() -> None:
    """自检：判据必须真的抓得住线上那一次的形态，且不误伤正常正文。"""
    assert echoes_reference(_ECHOED, _CONVERSATION) is True
    assert echoes_reference(_PRD_BODY, _CONVERSATION) is False
    # 参考材料为空时什么都不能判——没有照抄对象的「复述」是伪命题
    assert echoes_reference(_ECHOED, "") is False
    # 太短的开头不作为证据（「好的」「收到」在任何文本里都可能出现）
    assert echoes_reference("好的", _CONVERSATION) is False


def test_echoed_reply_never_reaches_done() -> None:
    """两次都复述：必须一条 `done` 都不发（否则失败收尾会把它当「部分完成」落库）。"""
    llm = _ScriptedLLM([_ECHOED, _ECHOED])
    events = _drain(DocumentService(llm=llm), [{"role": "ai", "content": _ECHOED}])

    assert not [event for event in events if event.type == "done"]
    errors = [event for event in events if event.type == "error"]
    assert len(errors) == 1
    assert errors[0].code == "MODEL_OUTPUT_NOT_DOCUMENT"
    # 两次调用都发生了（重试确实跑过，不是原地放弃）
    assert len(llm.calls) == 2
    # 第二次带上了硬提示：不能原样重跑一遍原提示词
    second = llm.calls[1]
    assert any("复述" in str(item.content) for item in second[-1:]), second


def test_first_echo_then_good_document_still_succeeds() -> None:
    """第一次复述、第二次正常：整条链路照常收尾，`done` 里是那份正常正文。"""
    llm = _ScriptedLLM([_ECHOED, _PRD_BODY])
    events = _drain(DocumentService(llm=llm), [{"role": "ai", "content": _ECHOED}])

    done = [event for event in events if event.type == "done"]
    assert len(done) == 1
    assert done[0].data["reply"] == _PRD_BODY
    # 复述那一段被 `part{replace}` 清过一次，最终正文不会是两段拼接
    assert _ECHOED not in done[0].data["reply"]
    assert not [event for event in events if event.type == "error"]


@pytest.mark.parametrize("reference", ["", _CONVERSATION])
def test_normal_document_is_untouched(reference: str) -> None:
    """正常正文一次过：不重试、不报错（参考材料有没有都不影响）。"""
    llm = _ScriptedLLM([_PRD_BODY])
    history = [{"role": "ai", "content": reference}] if reference else []
    events = _drain(DocumentService(llm=llm), history)

    assert len(llm.calls) == 1
    assert [event.data["reply"] for event in events if event.type == "done"] == [_PRD_BODY]
