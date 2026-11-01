"""大段文本的统一上限（v8）：**60KB**。

## 约束的是什么

三列大文本：`document_versions.content`（整篇 PRD）、`generation_jobs.draft_content`
（分章草稿）、`generation_jobs.completed_sections`（章节清单 JSON）。

> 历史上还有第四列 `solutions.data`（会话快照 JSON），但它退役之后就**没有接进这道
> 闸门**：`solution_repo.create` 只写空串、`update` 刻意不碰它，
> `ensure_text_within_limit` 对它零调用。把「受约束的四列」写成事实是一句自相矛盾的
> 注释——没有读者会因它超长而失败。该列已在 v8 按 ADR-096 删除，这里只留三列。

它们的上限是 **60 × 1024 字节**，不是字符。理由是中文的字节数是字符数的 3 倍：
「6 万字」听起来不多，按 UTF-8 是 180KB。按字符设限会让同一条规则在中文与英文上
给出三倍的容量差，而真正决定「能不能存下」的是字节。

## 为什么闸门在这里，而不是靠列类型

列类型是**容量**，这里是**约束**。两者不该混为一谈：

- 列类型仍然给到 `MEDIUMTEXT`（见 `models._BIG_TEXT`）。它不是"允许 16MB"的意思，
  而是"别让类型成为最后一道报错的地方"——万一某条写入路径漏了校验，16MB 的余量
  让人还有机会在日志里发现，而不是当场 `Data too long`。
- 60KB 由一个常量执行，所有写入路径调同一个函数。**上限只有一个真源**。

## 为什么超了要报错而不是截断

截断会静默丢内容，而正文是用户唯一在乎的产物。报错会让调用方知道"这次没存进去"，
比"存进去了一半"好。

## 快速路径

`persist_progress` 每 300ms 写一次草稿，正文可达几十 KB。每次都 `encode("utf-8")`
是没有必要的开销，所以先按**字符数**粗筛：UTF-8 单字符最多 4 字节，字符数
不超过 `MAX_TEXT_BYTES // 4` 时必然不超限，直接返回；只有落进中间地带才真的编码。
"""

from __future__ import annotations

from backend.core.errors import AppError, ErrorCode

# 60KB（字节）。这个数不是从数据库类型倒推的，是产品决定：约 2 万汉字。
MAX_TEXT_BYTES = 60 * 1024

# 单字符最多的 UTF-8 字节数（4 字节平面）。用于快速路径的上界估计。
_MAX_BYTES_PER_CHAR = 4


def text_size(value: str) -> int:
    """字符串的 UTF-8 字节数。"""
    return len(value.encode("utf-8"))


def ensure_text_within_limit(value: str | None, *, field: str) -> None:
    """检查一段文本是否在 60KB 以内；超了抛 `AppError`。

    Args:
        value: 待写入的文本；`None` 视为空、直接通过。
        field: 出错时给人看的字段名（如「草稿正文」），会进错误文案。

    Raises:
        AppError: 内容超过 `MAX_TEXT_BYTES`。错误码用 `INVALID_REQUEST`——
            错误码清单刻意保持在 9 个（见 `core/errors.py`），这里不新增一个码：
            对前端而言「内容太长」与「请求不合法」的处理是同一个分支（提示并让用户改），
            多一个码只会增加两侧同步的负担。
    """
    if not value:
        return
    # 快速路径：字符数 * 4 都不超上限时，必然不超（无需编码几十 KB）
    if len(value) <= MAX_TEXT_BYTES // _MAX_BYTES_PER_CHAR:
        return

    size = text_size(value)
    if size > MAX_TEXT_BYTES:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"{field}超过 60KB 上限（当前 {size:,} 字节，上限 {MAX_TEXT_BYTES:,} 字节），"
            "请精简或拆分后再保存",
        )


__all__ = ["MAX_TEXT_BYTES", "ensure_text_within_limit", "text_size"]
