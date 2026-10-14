"""火山方舟流式封装的用量映射（`services/ark_llm.py`）测试。

方舟的用量有一个**只在真机上才会暴露**的陷阱：开启 `include_usage` 后，token 计数
被放在「最后一个流式分块」的顶层 `usage` 上，而**那一帧的 `choices` 是空的**。
旧实现的循环以 `if not chunk.choices: continue` 开头，会把这整帧丢掉，症状是
「明明开了 include_usage 却永远拿不到 usage」——离线跑得天长地久也看不出来。
本文件把这几点钉住：

1. 必须下发 `stream_options={"include_usage": True}`，否则方舟流式根本不回吐用量；
2. 空 `choices` 的用量帧要被读出，并映射成 LangChain 的 `usage_metadata`；
3. 用量帧自身没有 `finish_reason`，必须把之前正文帧上记下的**带到末块**，
   否则上游的截断检测（读末块 `finish_reason`）永远判不出截断。

全部离线：把 `AsyncArk` 换成一个只重放预置分块的假客户端，不触网。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from backend.services import ark_llm
from backend.services.ark_llm import VolcanoArkChatModel, _usage_metadata


def _chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    usage: Any = None,
    with_choice: bool = True,
) -> Any:
    """构造一个形似方舟流式分块的对象（只需有 `.choices` / `.usage` 属性）。"""
    choices: list[Any] = []
    if with_choice:
        choices.append(
            SimpleNamespace(
                delta=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        )
    return SimpleNamespace(choices=choices, usage=usage)


def _usage(prompt: int, completion: int, total: int | None = None) -> Any:
    """构造形似 `CompletionUsage` 的对象。"""
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total if total is not None else prompt + completion,
    )


def _patch_ark(monkeypatch, chunks: list[Any]) -> dict:
    """把 `AsyncArk` 换成离线假客户端；返回收集到的调用参数。"""
    captured: dict = {}

    class _Completions:
        async def create(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)

            async def _gen():
                for chunk in chunks:
                    yield chunk

            return _gen()

    class _Client:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            self.chat = SimpleNamespace(completions=_Completions())

        async def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(ark_llm, "AsyncArk", _Client)
    return captured


def _drain(model: VolcanoArkChatModel) -> list:
    """跑完一次流式调用，收集全部分块。"""

    async def run() -> list:
        return [chunk async for chunk in model.astream([])]

    return asyncio.run(run())


def _model(**kwargs: Any) -> VolcanoArkChatModel:
    return VolcanoArkChatModel(api_key="k", model="m", **kwargs)


# ---------------------------------------------------------------------------
# 请求参数
# ---------------------------------------------------------------------------


def test_stream_requests_usage(monkeypatch) -> None:
    """必须下发 `include_usage`，否则方舟流式不回吐任何 token 计数。"""
    captured = _patch_ark(monkeypatch, [_chunk(content="你好", finish_reason="stop")])

    _drain(_model())

    assert captured["stream_options"] == {"include_usage": True}
    assert captured["stream"] is True


def test_client_is_closed_after_stream(monkeypatch) -> None:
    """流结束后必须关闭客户端，避免长生命周期连接池在请求间泄漏。"""
    captured = _patch_ark(monkeypatch, [_chunk(content="x", finish_reason="stop")])

    _drain(_model())

    assert captured.get("closed") is True


def test_thinking_disabled_by_default_and_overridable(monkeypatch) -> None:
    """默认关闭官方思考（首字更快）；显式开启时不下发 thinking 参数。"""
    default = _patch_ark(monkeypatch, [_chunk(content="x", finish_reason="stop")])
    _drain(_model())
    assert default["thinking"] == {"type": "disabled"}

    enabled = _patch_ark(monkeypatch, [_chunk(content="x", finish_reason="stop")])
    _drain(_model(thinking_enabled=True))
    assert "thinking" not in enabled


def test_max_tokens_forwarded_only_when_set(monkeypatch) -> None:
    """`max_tokens` 只在配置了时才下发（留空交给供应商默认值）。"""
    unset = _patch_ark(monkeypatch, [_chunk(content="x", finish_reason="stop")])
    _drain(_model())
    assert "max_tokens" not in unset

    set_ = _patch_ark(monkeypatch, [_chunk(content="x", finish_reason="stop")])
    _drain(_model(max_tokens=123))
    assert set_["max_tokens"] == 123


# ---------------------------------------------------------------------------
# 用量帧与结束原因（真机陷阱所在）
# ---------------------------------------------------------------------------


def test_usage_frame_with_empty_choices_is_captured(monkeypatch) -> None:
    """用量帧的 `choices` 为空，仍要被读出并映射——旧实现会整帧丢掉。"""
    chunks = [
        _chunk(content="你好"),
        _chunk(content="世界", finish_reason="stop"),
        # 关键：携带用量、但 choices 为空的那一帧
        _chunk(usage=_usage(11, 22), with_choice=False),
    ]
    _patch_ark(monkeypatch, chunks)

    out = _drain(_model())

    usage_chunks = [chunk for chunk in out if chunk.usage_metadata]
    assert len(usage_chunks) == 1
    assert usage_chunks[0].usage_metadata == {
        "input_tokens": 11,
        "output_tokens": 22,
        "total_tokens": 33,
    }


def test_finish_reason_survives_onto_last_chunk(monkeypatch) -> None:
    """用量帧自身无 `finish_reason`，必须把之前记下的带到末块（截断检测依赖它）。"""
    chunks = [
        _chunk(content="半截"),
        _chunk(content="", finish_reason="length"),
        _chunk(usage=_usage(3, 1), with_choice=False),
    ]
    _patch_ark(monkeypatch, chunks)

    out = _drain(_model())

    assert out[-1].response_metadata.get("finish_reason") == "length"
    # 末块即用量帧，内容为空但带上了用量
    assert out[-1].content == ""
    assert out[-1].usage_metadata is not None


def test_text_chunks_are_emitted_in_order(monkeypatch) -> None:
    """正文分块按序产出，拼接结果与模型输出一致。"""
    chunks = [
        _chunk(content="第一段"),
        _chunk(content="第二段"),
        _chunk(content="", finish_reason="stop"),
        _chunk(usage=_usage(2, 2), with_choice=False),
    ]
    _patch_ark(monkeypatch, chunks)

    out = _drain(_model())

    assert "".join(chunk.content for chunk in out) == "第一段第二段"


# ---------------------------------------------------------------------------
# 用量映射函数
# ---------------------------------------------------------------------------


def test_usage_metadata_maps_field_names() -> None:
    """方舟字段映射到 LangChain 统一键名；缺字段按零补。"""
    assert _usage_metadata(_usage(5, 6)) == {
        "input_tokens": 5,
        "output_tokens": 6,
        "total_tokens": 11,
    }
    # 只给了 prompt_tokens 时，其余按零补，total 退化为已有部分之和
    assert _usage_metadata(SimpleNamespace(prompt_tokens=5)) == {
        "input_tokens": 5,
        "output_tokens": 0,
        "total_tokens": 5,
    }


def test_usage_metadata_returns_none_when_absent() -> None:
    """完全没有用量信息时返回 `None`：『没给用量』与『用量为零』是两回事。"""
    assert _usage_metadata(None) is None
    assert _usage_metadata(SimpleNamespace()) is None
