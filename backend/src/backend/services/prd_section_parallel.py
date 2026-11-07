"""PRD 并行车道：撰写按章序，审核 / 改写按章并发，**单章失败不终止全篇**。

为什么单独成模块
----------------
分章撰写器（`prd_section_writer.py`）的并行链路从前是 `_stream_parallel` 里的
376 行闭包集合：撰写车道、审核重试、改写任务、失败聚合与收尾全挤在同一个方法里，
「单章失败不终止全篇」这条规则被埋在一串嵌套函数中间。拆成独立模块后：

- 车道状态（`_SectionState`）与三条子流程各占一个方法：先读 `_lane()` 的执行顺序，
  再按需下钻到 `_write_section()` / `_section_task()` / `_review_with_retry()`；
- 撰写器只保留串行链路与两条链路共享的契约（消息组装、审核调用、事件构造）——
  那段重复的初始化收在 `PrdSectionWriter._prepare_inputs()` 一处。

依赖方向是单向的：本模块依赖撰写器里的共享契约；撰写器只在 `_stream_parallel()`
里**延迟导入**本模块，避免模块级循环导入。

行为契约（与串行链路同形，勿改）
--------------------------------
- `chunk`：逐字增量，**必须带 `section_id` + `index`**——多章同时吐字，没有归属
  就只能整批丢弃（那正是「PRD 不流式」的历史成因）。重试会重吐本章，因此每次尝试
  都**重报一次 `writing`**，前端据此清该章缓冲。
- `part`：一段正文一被定稿就整段下发，**可乱序到达**；每段带 `index`，
  `done.reply` 由本模块按 index 装配，前端按 same index 归位。
- 单章失败：撰写用尽重试 → 写占位继续（`part.failed=true`、`done.partial=true`，
  `failed_sections` 列明原因）；审核用尽 → 保留正文；改写用尽 → 保留改写前版本；
  只有全部章节失败才发 `error`。
- 所有模型调用共用一个信号量（`PRD_PIPELINE_CONCURRENCY`），计量用 `use_step`
  归因；`AppError`（预算超顶 / 墙钟硬顶）不重试、直接走致命路径。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.messages import BaseMessage

from backend.core.contracts import StreamEvent
from backend.core.errors import AppError
from backend.services.document_review import review_data
from backend.services.llm_meter import use_step
from backend.services.pending_items import PendingBlockSuppressor, extract
from backend.services.prd_section_writer import (
    PRD_SECTIONS,
    PrdSection,
    SectionReview,
    _ensure_heading,
    _join_prior,
    _part_event,
    _section_chunk_event,
    _section_stage_event,
    build_document_header,
)
from backend.services.stream_reply import error_event

if TYPE_CHECKING:
    from backend.services.prd_section_writer import PrdSectionWriter, _PreparedInputs

logger = logging.getLogger(__name__)


@dataclass
class _SectionState:
    """并行车道里单章的运行态（只活在单次 `stream()` 的生命周期里）。"""

    index: int
    section: PrdSection
    status: str = "pending"  # pending / writing / reviewing / rewriting / done / failed
    draft: str = ""
    part: str = ""
    failed: bool = False
    reason: str = ""
    review: SectionReview | None = None
    # 本章是否真的改写并采用了新正文（汇总结论的 `rewritten` 如实取它）
    rewritten: bool = False
    pending_items: list[dict[str, Any]] = field(default_factory=list)


def _failed_placeholder(section: PrdSection, reason: str) -> str:
    """生成失败的章节占位：保留标题结构，并明确写出失败原因。"""
    return (
        f"{section.heading}\n\n"
        f"> 本章生成失败：{reason}。请稍后重新生成本章以补齐本节。\n\n"
    )


class ParallelSectionRun:
    """一次三车道并行生成的全部运行态与流程。

    撰写**严格按章序**（后续章节要引用前序定稿），审核 / 改写以章节任务并发执行。
    三条车道全部由 `_lane()` 串起来；单章失败的处理放在 `_write_section()` 里，
    因此「失败写占位、但不 `return`、继续下一章」这条规则一眼可见。
    """

    def __init__(self, writer: PrdSectionWriter, prepared: _PreparedInputs) -> None:
        self._writer = writer
        self._prepared = prepared
        self._total = len(PRD_SECTIONS)
        self._parts: dict[int, str] = {}
        self._drafts: list[str] = [""] * self._total
        self._states = [
            _SectionState(index=index, section=section)
            for index, section in enumerate(PRD_SECTIONS)
        ]
        self._queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        self._sem = asyncio.Semaphore(writer._concurrency)
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # 对外入口：事件流
    # ------------------------------------------------------------------

    async def stream(self) -> AsyncIterator[StreamEvent]:
        """产出整次并行生成的事件流；消费者断开时取消车道与全部派生任务。"""
        total = self._total
        yield StreamEvent(
            type="status",
            content=f"正在并行生成 {total} 章 PRD…",
            data={"doc_type": "prd", "total_sections": total, "parallel": True},
        )

        # 文档头由代码产出：它不属于任何一章，不能被「改写某章」波及。
        # 走 part 事件而不是 chunk，前端据此把它归入「已定稿部分」。
        header = build_document_header(self._prepared.name)
        yield _part_event(header)
        self._parts[-1] = header

        driver = asyncio.create_task(self._lane())
        try:
            while True:
                event = await self._queue.get()
                if event is None:
                    break
                yield event
        finally:
            if not driver.done():
                driver.cancel()
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(driver, *list(self._tasks), return_exceptions=True)

    async def _emit(self, event: StreamEvent) -> None:
        """把事件交给消费端；队列无界，`await` 永不阻塞。"""
        await self._queue.put(event)

    # ------------------------------------------------------------------
    # 三车道
    # ------------------------------------------------------------------

    async def _lane(self) -> None:
        """撰写车道：按章序推进；每章写完立刻派生审核 / 改写任务。

        ⚠️ 这里的 `except` 是**整条并行链路**的兜底：致命错误（`AppError` 等）转成
        `error` 事件，已流出的内容保留在前端；单章失败不走到这里——它在
        `_write_section()` 里就地降级成占位。
        """
        try:
            for idx, section in enumerate(PRD_SECTIONS):
                st = self._states[idx]
                st.status = "writing"
                prior = _join_prior([draft for draft in self._drafts[:idx] if draft])
                draft = await self._write_section(idx, section, prior)
                if draft is None:
                    # 已写占位并落过 `part`：继续下一章，绝不能终止全篇
                    continue
                self._drafts[idx] = draft
                st.draft = draft
                task = asyncio.create_task(self._section_task(idx, draft, prior))
                self._tasks.add(task)
                # ⚠️ 刻意**不**在完成时把任务摘出集合：`gather` 只收集合里还在的任务，
                # 先完成（尤其是先失败）的会被摘掉，异常随之无人回收——表现为
                # 「预算超顶本该致命，却在没有任何告警的情况下产出一份缺章的文档」。
                # 六个任务的开销可以忽略，让 `gather` 无论如何都收得到它们。

            if self._tasks:
                await asyncio.gather(*list(self._tasks))

            await self._finish()
        except Exception as exc:  # noqa: BLE001 - 致命失败统一转 error 事件
            await self._emit(error_event(exc))
        finally:
            await self._queue.put(None)

    async def _write_section(self, idx: int, section: PrdSection, prior: str) -> str | None:
        """撰写一章，失败按 `PRD_SECTION_MAX_RETRIES` 重试。

        Returns:
            非空草稿；用尽重试仍失败时已写出失败占位与 `part`，返回 `None`
            （调用方据此 `continue`，**不是**终止全篇）。
        """
        draft = ""
        last_exc: Exception | None = None
        for attempt in range(self._writer._max_retries + 1):
            # ⚠️ `writing` 每次尝试都要重报，不能只在循环外报一次：
            # 重试会从头把本章重吐一遍，前端**收到 `writing` 才清掉**
            # 上一轮已吐出的半截。少报一次，两轮的字就在卡片里叠着。
            await self._emit(_section_stage_event("writing", section, idx, self._total))
            try:
                draft = await self._stream_section_text(
                    self._writer._section_messages(
                        section=section,
                        section_template=self._prepared.template_sections.get(
                            section.heading, ""
                        ),
                        system_prompt=self._prepared.system_prompt,
                        requirements=self._prepared.requirements_text,
                        conversation=self._prepared.conversation,
                        field_contract=self._prepared.field_contract,
                        prior_sections=prior,
                    ),
                    "writing",
                    section,
                    idx,
                )
                break
            except AppError:
                # 预算超顶 / 墙钟硬顶：重来多少次都一样，直接走致命路径
                raise
            except Exception as exc:  # noqa: BLE001 - 单章撰写失败可重试
                last_exc = exc
                if attempt >= self._writer._max_retries:
                    break
                # 中间失败此前完全静默：重试发生在模型调用层（stream_reply 已记），
                # 但「哪一章、第几次」的归属只有这里知道，补一条补上这条链。
                logger.warning(
                    "章节 %s 撰写失败，将重试（第 %d 次）",
                    section.id,
                    attempt + 1,
                    extra={"event": "prd.section_write_retry"},
                )

        if draft.strip():
            return draft

        st = self._states[idx]
        st.status = "failed"
        st.failed = True
        st.reason = f"撰写连续失败：{last_exc}" if last_exc else "撰写结果为空"
        st.part = _failed_placeholder(section, st.reason)
        self._parts[idx] = st.part
        await self._emit(
            _section_stage_event("failed", section, idx, self._total, note=st.reason)
        )
        await self._emit(
            _part_event(
                st.part,
                section=section,
                index=idx,
                total=self._total,
                failed=True,
            )
        )
        return None

    async def _section_task(self, idx: int, draft: str, prior: str) -> None:
        """审核 +（可选）改写一章，然后定稿并发 `part`。

        与串行链路同口径：审一次 → 至多改一次，**改写后不再复审**；单章的审核 /
        改写失败各自降级（保留正文 / 保留改写前版本），不判失败。
        """
        st = self._states[idx]
        review: SectionReview | None = None
        if self._writer._review_enabled:
            st.status = "reviewing"
            await self._emit(
                _section_stage_event("reviewing", st.section, idx, self._total)
            )
            review = await self._review_with_retry(st.section, draft, prior)
            st.review = review
            # `max_rewrites` 为 0 时仍是「只审不改」的有效开关；> 0 时最多改一次
            if (
                review is not None
                and self._writer._max_rewrites >= 1
                and review.triggers_rewrite(self._writer._rewrite_min_severity)
            ):
                st.status = "rewriting"
                await self._emit(
                    _section_stage_event(
                        "rewriting",
                        st.section,
                        idx,
                        self._total,
                        note=review.summary(),
                        issues=review.issues,
                    )
                )
                try:
                    new_draft = await self._stream_section_text(
                        self._writer._rewrite_messages(
                            section=st.section,
                            section_text=draft,
                            system_prompt=self._prepared.system_prompt,
                            writing_rules=self._prepared.writing_rules,
                            requirements=self._prepared.requirements_text,
                            prior_sections=prior,
                            review=review,
                        ),
                        "rewriting",
                        st.section,
                        idx,
                    )
                except AppError:
                    raise
                except Exception:  # noqa: BLE001 - 改写失败保留改写前版本
                    logger.warning(
                        "章节 %s 改写失败，保留改写前版本",
                        st.section.id,
                        exc_info=True,
                        extra={"event": "prd.rewrite_failed"},
                    )
                    new_draft = ""
                if new_draft and new_draft.strip():
                    draft = new_draft
                    st.rewritten = True

        text = _ensure_heading(draft, st.section.heading)
        clean_text, items = extract(text, id_prefix=st.section.id)
        st.pending_items = items
        if clean_text:
            st.status = "done"
            st.part = f"{clean_text}\n\n"
        else:
            # 兜底：只剩标记块、剥离后没有正文时，仍要留下标题结构与失败说明
            st.status = "failed"
            st.failed = True
            st.reason = "正文为空"
            st.part = _failed_placeholder(st.section, st.reason)
        self._parts[idx] = st.part
        await self._emit(
            _part_event(
                st.part,
                section=st.section,
                index=idx,
                total=self._total,
                failed=st.failed,
            )
        )

    async def _review_with_retry(
        self, section: PrdSection, section_text: str, prior_sections: str
    ) -> SectionReview | None:
        """审核一章，失败按 `PRD_SECTION_MAX_RETRIES` 重试；用尽后按通过处理。"""
        for attempt in range(self._writer._max_retries + 1):
            try:
                async with self._sem:
                    with use_step("reviewing"):
                        return await self._writer._review(
                            section=section,
                            section_text=section_text,
                            writing_rules=self._prepared.writing_rules,
                            requirements=self._prepared.requirements_text,
                            prior_sections=prior_sections,
                        )
            except AppError:
                # 预算超顶 / 墙钟硬顶：重来多少次都一样，直接走致命路径
                raise
            except Exception:  # noqa: BLE001 - 单章审核失败可重试
                if attempt >= self._writer._max_retries:
                    logger.warning(
                        "章节 %s 审核连续失败，保留正文不判失败",
                        section.id,
                        exc_info=True,
                        extra={"event": "prd.review_exhausted"},
                    )
                    return None
                # 审核中间失败此前静默：补一条，让「哪一章审了几次才放弃」可查。
                logger.warning(
                    "章节 %s 审核失败，将重试（第 %d 次）",
                    section.id,
                    attempt + 1,
                    extra={"event": "prd.review_retry"},
                )
        return None

    async def _stream_section_text(
        self, messages: list[BaseMessage], step: str, section: PrdSection, index: int
    ) -> str:
        """流式产出某一章：边吐字边打章节标签，返回整章原文。

        与串行链路用同一套抑制器（`PendingBlockSuppressor`）：待确认块一旦开始，
        后面的字就要先扣住，否则用户会看见它一闪一闪地跳。

        吐出去的是**模型原始输出**，不是定稿正文——补标题、补章间空行都发生在
        定稿时。前端只把它当「过程中的显示」，`part` 一到就覆盖。
        """
        buffer: list[str] = []
        suppressor = PendingBlockSuppressor()
        # ⚠️ 信号量只包一次模型调用：持有它再等别的章节任务会死锁。
        # 循环里唯一的 await 是 `_emit`（无界队列，永不阻塞），所以安全。
        async with self._sem:
            with use_step(step):
                async for delta in self._writer._stream_text(messages):
                    buffer.append(delta)
                    clean = suppressor.feed(delta)
                    if clean:
                        await self._emit(_section_chunk_event(clean, section, index))
                trailing = suppressor.flush()
                if trailing:
                    await self._emit(_section_chunk_event(trailing, section, index))
        return "".join(buffer)

    async def _finish(self) -> None:
        """收尾：失败章节聚合、审核结论、`stage:done` 与 `done`（按 index 装配正文）。"""
        total = self._total
        failed_sections = [
            {
                "section_id": st.section.id,
                "title": st.section.title,
                "index": st.index,
                "reason": st.reason,
            }
            for st in self._states
            if st.failed
        ]
        if len(failed_sections) == total:
            raise RuntimeError("全部章节生成失败")

        issues: list[dict[str, Any]] = []
        for st in self._states:
            if st.review and st.review.needs_revision:
                for issue in st.review.issues:
                    issues.append(
                        {
                            "section_id": st.section.id,
                            "section_title": st.section.title,
                            "severity": str(issue.get("severity") or "medium"),
                            "detail": str(issue.get("detail") or ""),
                        }
                    )
        # 没开审核就不发结论：这一轮没有任何章节被核对过，报一句
        # 「0 个待处理问题」会被读成「审过了、没问题」，那是假的。
        if self._writer._review_enabled:
            verdict = "revise" if issues else "pass"
            conclusion = (
                f"{total - len(failed_sections)} 章完成，{len(issues)} 个待处理问题"
            )
            await self._emit(
                StreamEvent(
                    type="review",
                    content=conclusion,
                    data=review_data(
                        "prd",
                        verdict=verdict,
                        summary=conclusion,
                        issues=issues,
                        # 如实汇总：任意一章真的改写并采用了新正文就算改过。
                        # 从前这里恒为 False，于是「PRD 能不能修复」随并发度配置漂。
                        rewritten=any(st.rewritten for st in self._states),
                        failed_sections=failed_sections,
                    ),
                )
            )

        pending_items = [item for st in self._states for item in st.pending_items]
        reply = (
            self._parts[-1] + "".join(self._parts[i] for i in sorted(self._parts) if i >= 0)
        ).strip()
        await self._emit(
            StreamEvent(
                type="stage",
                content="PRD 已生成",
                data={"stage": "done", "index": total, "total": total},
            )
        )
        await self._emit(
            StreamEvent(
                type="done",
                content="",
                data={
                    "doc_type": "prd",
                    "reply": reply,
                    "pending_items": pending_items,
                    "partial": bool(failed_sections),
                    "failed_sections": failed_sections,
                },
            )
        )
