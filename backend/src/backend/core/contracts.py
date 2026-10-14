"""跨层共享的数据契约。

这里存放 services 与 api 共享的数据契约，放在 core 是为了避免 services
反向依赖 api：`services/` 只应依赖 `core/`，不应回头 import `api/`。
`api/schemas.py` 从这里 re-export 这三个模型，因此 API 层对外的引用
（`from backend.api.schemas import StreamEvent / QuestionsConfig`）保持不变。

本模块只允许依赖 `pydantic` / `typing`，**不得 import `backend.api.*`**——
一旦出现这种 import，就说明契约没沉到位，分层又被打破了。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ============================================================================
# 问题配置（对应 core/question_config.json）
# ============================================================================


class QuestionConfig(BaseModel):
    """问卷中的单个问题，字段与前端 `types/index.ts` 的 `QuestionConfig` 保持一致。"""

    name: str = Field(description="字段名称，作为提交答案时字典的 key")
    label: str = Field(description="展示给用户的题目文案")
    type: Literal["text", "textarea", "select", "radio"] = Field(
        description="题型：单行文本 / 多行文本 / 下拉选择 / 单选"
    )
    required: bool = Field(default=False, description="是否必填")
    options: list[str] = Field(default_factory=list, description="选择类题型的选项文案列表")


class QuestionsConfig(BaseModel):
    """问卷配置整体结构，按基础题 / 进阶题分组。"""

    base_questions: list[QuestionConfig] = Field(description="基础问题，创建会话时必答")
    advanced_questions: list[QuestionConfig] = Field(description="进阶问题，可按需补充")


# ============================================================================
# SSE 流式响应
# ============================================================================


class StreamEvent(BaseModel):
    """SSE 推送的单个事件。

    事件类型与 SSE 的 `event:` 行一一对应，前端既可以用
    `addEventListener('<type>')` 监听，也可以只解析 `data` 里的 `type` 字段。
    """

    type: Literal[
        "status",
        "chunk",
        "part",
        "done",
        "error",
        "stage",
        "tool_call",
        "document",
        "snapshot",
        "run_summary",
    ] = Field(
        description=(
            "事件类型：状态提示 status / 增量文本 chunk / 定稿正文 part / 阶段进度 stage / "
            "结束 done / 错误 error，tool_call 与 document 为后续扩展预留。"
            "`stage` 用于分章节生成：`data.stage` 取 writing / reviewing / rewriting / done，"
            "`data.section_id` 标识当前章节（前端据此重置本章的实时缓冲）。"
            "`part` 也用于分章节生成：每段正文（文档头、每一章）一被定稿就整段下发，"
            "`data.text` 可直接追加到文档末尾，前端据此拼出与最终 `done.reply` 逐字一致的预览。"
            "`snapshot` 与 `run_summary` 用于生成任务（generation_jobs）：前者在每次订阅开始时 "
            "**首发给**，`data` 含 job_id/kind/status/phase/finalized_text/live_buffer/"
            "completed_sections/solution_id/error_code/error_message，供前端补全已产出正文；"
            "后者在任务收尾时下发最小摘要 `data={kind, status, duration_ms}`。"
        )
    )
    content: str = Field(default="", description="文本内容（增量或提示语）")
    data: dict[str, Any] = Field(default_factory=dict, description="事件附加数据")
    code: str | None = Field(
        default=None,
        description=(
            "机器可读的错误码（`backend/core/errors.py` 的 `ErrorCode` 成员），"
            "供前端做分支判断。只有 `error` 事件会带，其余事件恒为 `None`。"
            "刻意放在**顶层**而不是塞进 `data`：SSE 流是 HTTP 200，前端拿不到状态码，"
            "判断「该不该降级重试」只能靠这个字段，放在顶层才不会被 `data` 的"
            "其它附加字段淹没。缺失或取值未知时前端一律按 `INTERNAL_ERROR` 处理。"
        ),
    )
