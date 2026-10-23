"""把某个账号的数据打成一个 ZIP（v4.5 T5-4 数据合规）。

为什么要「给人看的」和「给机器读的」各留一份
============================================
合规导出的两种用法完全不同：用户/审核人打开包是想**读**（所以正文与对话必须是
`.md`，不是转义字符串），而迁移或复核脚本要的是结构化数据（所以有 `solutions.json`
与 `jobs.csv`）。只给 JSON，人得自己找地方粘出来看；只给 markdown，程序没法用。

包内结构::

    manifest.json                    导出时间、账号、各类条数、口径说明
    account.json                     账号信息（**不含密码哈希与 salt**）
    solutions.json                   方案摘要 + 文档元信息 + 线程清单
    documents/<序号-方案>/<kind>.md   三份产物正文（当前版本）
    chats/<序号-方案>/<stage>.md      各线程对话转录
    jobs.csv                         生成任务记录（UTF-8 BOM，Excel 直接打开）

三条硬纪律
==========
1. **绝不导出密码材料**：`password_hash` / `salt` 不进包。
2. **绝不导出别人的东西**：所有读取都按 `user_id` 过滤，审计表不出包（里面含操作者）。
3. **文件名必须消毒**：方案标题是用户自己输入的，含 `/`、`..`、`\x00` 时会在 ZIP 里
   造出**路径穿越**——解压方（可能是另一台机器上的审核人）的文件会被写到目标目录之外。
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import asdict
from typing import Any

from backend.core.clock import utc_ms_text, utc_now
from backend.core.engine import session_scope
from backend.core.errors import AppError, ErrorCode
from backend.core.models import ChatTurn
from backend.repositories.account_repo import AccountDataRepository
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.repositories.document_repo import DocumentRepository
from backend.repositories.solution_repo import SolutionRepository

# 单次导出的体积上限。ZIP 在内存里拼，不设上限时一个「方案特别多」的账号
# 可以把进程内存吃满——而导出是低频操作，为它拖垮整个服务不值得。
# 超过时明确拒绝并说明原因，比 OOM 后再也起不来好。
MAX_EXPORT_BYTES = 50 * 1024 * 1024

# 文件名/目录名里必须替换掉的字符：Windows 非法字符 + 所有控制字符。
# 反斜杠也在其中——它既是 Windows 路径分隔符，也会在部分解压器里被当分隔符。
_UNSAFE_PATH = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Windows 保留设备名。叫 `con.md` 的文件在 Windows 上根本创建不出来，
# 解压会失败——标题恰好是这些词虽然罕见，但代价（整个包解不开）远大于一行判断。
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

# 产物类型 → 导出文件名。用连字符而不是下划线：这是给人看的文件名。
_KIND_FILES = {"prd": "prd.md", "api_docs": "api-docs.md", "prompts": "prompts.md"}

# 线程名（`stage`）→ 导出时的中文标题。未知线程原样用 `stage`，不丢数据。
_STAGE_TITLES = {
    "requirements": "需求澄清",
    "chat": "AI 对话",
    "prd": "PRD 澄清",
    "api_docs": "接口文档澄清",
    "prompts": "提示词澄清",
    "clarify": "文档澄清",
    "auto-clarify": "AI 自行澄清",
}


def _safe_name(text: str, fallback: str, *, limit: int = 48) -> str:
    """把任意文本变成安全的单层文件名（不含任何路径分隔符）。

    `..` 单独处理：只替换 `/` 与 `\\` 是不够的——`a/../b` 里的 `..` 在
    「只剥斜杠」的实现下会变成 `a_.._b`（幸好），但如果实现改成保留路径结构，
    `..` 立刻就是穿越。这里一并抹掉，让「文件名里出现 `.` 序列」这件事不可能成立。
    """
    cleaned = _UNSAFE_PATH.sub("_", (text or "").strip())
    cleaned = cleaned.replace("..", "_")
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Windows 不允许文件名以点或空格结尾（解压时会静默截断，造成重名）
    cleaned = cleaned.strip(" .")
    if not cleaned or cleaned.lower() in _RESERVED_NAMES:
        return fallback
    return cleaned[:limit].strip(" .") or fallback


def _dump_json(payload: Any) -> str:
    """统一的 JSON 落盘格式：不转义中文、缩进 2、保留原顺序。

    `ensure_ascii=False` 是必须的：转义成 `\\u4e2d` 之后，中文用户在编辑器里
    打开看到的就是一屏乱码，导出等于没导。
    """
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def _decode_list(raw: Any) -> list[Any]:
    """把库里的 JSON 文本解回列表；坏数据按空列表处理（导出不该因一行脏数据失败）。"""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _render_asks(asks: list[Any], answers: list[Any]) -> list[str]:
    """把一轮里的结构化选择题渲染成 markdown（含当时选了什么）。

    这道题**只在数据里**：前端靠 `asks` 渲染卡片，正文里不重复题干。导出时若只写
    `content`，用户会看到「请确认以下两点」后面什么都没有——像是数据丢了。
    """
    chosen: dict[str, dict[str, Any]] = {}
    for item in answers:
        if isinstance(item, dict) and isinstance(item.get("ask_id"), str):
            chosen[item["ask_id"]] = item

    lines: list[str] = []
    for ask in asks:
        if not isinstance(ask, dict):
            continue
        question = str(ask.get("question") or "").strip()
        if not question:
            continue
        answer = chosen.get(str(ask.get("id") or ""))
        picked = set(answer.get("labels") or []) if answer else set()
        lines.append(f"- **{question}**")
        background = str(ask.get("background") or "").strip()
        if background:
            lines.append(f"  > {background}")
        for option in ask.get("options") or []:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "")
            text = str(option.get("text") or "")
            mark = " ✓" if label and label in picked else ""
            description = str(option.get("description") or "").strip()
            suffix = f"（{description}）" if description else ""
            lines.append(f"  - {label}. {text}{suffix}{mark}")
        if ask.get("multiple"):
            lines.append("  - 可多选")
    return lines


def _render_stage(title: str, turns: list[ChatTurn]) -> str:
    """把一个线程的全部轮次渲染成一份可读的 markdown。"""
    lines = [f"# {title}", ""]
    if not turns:
        lines.append("_（本线程没有对话记录）_")
        return "\n".join(lines) + "\n"

    for turn in turns:
        role = str(turn.role or "")
        speaker = {"user": "用户", "assistant": "助手", "tool": "工具回执", "system": "系统"}.get(
            role, role or "未知"
        )
        moment = str(turn.created_at or "")
        flags: list[str] = []
        if turn.interrupted:
            flags.append("已停止")
        suffix = f"（{'、'.join(flags)}）" if flags else ""

        lines.append(f"## {speaker}{suffix}")
        if moment:
            lines.append(f"_{moment}（UTC）_")
        content = str(turn.content or "").strip()
        if content:
            lines.append("")
            lines.append(content)
        ask_lines = _render_asks(_decode_list(turn.asks), _decode_list(turn.answers))
        if ask_lines:
            lines.append("")
            lines.append("**请你确认：**")
            lines.extend(ask_lines)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class _ZipBuilder:
    """带体积上限的 ZIP 累加器。

    上限在**每次写入后**检查：只在结尾查一次的话，超限的那一次写入已经占满了内存，
    检查就成了事后通知。
    """

    def __init__(self) -> None:
        self._buffer = io.BytesIO()
        self._zip = zipfile.ZipFile(self._buffer, "w", zipfile.ZIP_DEFLATED)
        self._files = 0

    def add_text(self, name: str, text: str) -> None:
        self._zip.writestr(name, text)
        self._files += 1
        self._guard()

    def _guard(self) -> None:
        size = self._buffer.tell()
        if size > MAX_EXPORT_BYTES:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                f"这个账号的数据超过导出上限（{MAX_EXPORT_BYTES // (1024 * 1024)} MB）。"
                "请先用任务监控页确认数据规模，或分批清理后再导出。",
                413,
            )

    def finish(self) -> tuple[bytes, int]:
        self._zip.close()
        return self._buffer.getvalue(), self._files


def build_zip(user: dict[str, Any], *, exported_by: str) -> tuple[bytes, str, int]:
    """组装一个账号的导出包。

    Returns:
        `(zip 字节, 建议下载文件名, 包内文件数)`。文件名**只含 ASCII 安全字符**：
        它要进 `Content-Disposition`，中文名必须走 RFC 5987 的 `filename*`，
        而部分旧客户端不认——给一个纯 ASCII 的兜底名才不会出现「下载下来叫
        `download.bin`」。
    """
    user_id = int(user["id"])
    username = str(user["username"])
    with session_scope() as session:
        counts = AccountDataRepository(session).counts(user_id)
    builder = _ZipBuilder()

    # —— 账号信息 ——
    # 只写「关于这个账号本身」的字段。`password_hash` / `salt` 永不出现：
    # 导出包会被转发、拷贝、存进网盘，密码材料的哈希一旦流出，离线爆破的成本
    # 就只剩算力了。
    builder.add_text(
        "account.json",
        _dump_json(
            {
                "id": user_id,
                "username": username,
                "created_at": user.get("created_at"),
                "is_active": bool(user.get("is_active", 1)),
                "is_admin": bool(user.get("is_admin", 0)),
                "quota": {
                    "daily_task_limit": int(user.get("daily_task_limit", -1)),
                    "daily_token_limit": int(user.get("daily_token_limit", -1)),
                    "max_running": int(user.get("max_running", -1)),
                    "_说明": "-1 = 跟随全局配置，0 = 不限，N = 具体上限",
                },
                "_说明": "本文件不含密码哈希与 salt，也不含任何其他账号的数据",
            }
        ),
    )

    # —— 方案 / 文档 / 对话 ——
    # 仓储返回模型对象；导出是「顺着字段往下拼文件」的扁平逻辑，这里在边界上转成
    # dict / 投影的 asdict，下面的拼装代码保持原样（`solution["id"]` / `document.get(...)`）。
    with session_scope() as session:
        solutions = [
            item.model_dump() for item in SolutionRepository(session).list_by_owner(user_id)
        ]
    solution_summary: list[dict[str, Any]] = []

    for index, solution in enumerate(solutions, start=1):
        solution_id = int(solution["id"])
        title = str(solution.get("title") or "").strip() or f"未命名方案 {solution_id}"
        # 序号前缀顺带保证目录名唯一：两份同名方案不会互相覆盖
        folder = f"{index:02d}-{_safe_name(title, f'solution-{solution_id}')}"

        with session_scope() as session:
            documents = [
                asdict(item)
                for item in DocumentRepository(session).list_by_solution(
                    user_id, solution_id
                )
            ]
        doc_meta: list[dict[str, Any]] = []
        for document in documents:
            kind = str(document.get("kind") or "")
            content = str(document.get("content") or "")
            filename = _KIND_FILES.get(kind, f"{_safe_name(kind, 'document')}.md")
            builder.add_text(f"documents/{folder}/{filename}", content)
            doc_meta.append(
                {
                    "kind": kind,
                    "file": f"documents/{folder}/{filename}",
                    "version_no": document.get("version_no"),
                    "updated_at": document.get("updated_at"),
                    "pending_items": _decode_list(document.get("pending_items")),
                }
            )

        with session_scope() as session:
            turns = ChatTurnRepository(session).list_all(user_id, solution_id)
            stage_names: list[str] = []
            # 按出现顺序分组，不排序：线程顺序本身就是用户看到的顺序
            grouped: dict[str, list[ChatTurn]] = {}
            for turn in turns:
                grouped.setdefault(str(turn.stage or ""), []).append(turn)
            rendered = {
                stage: _render_stage(_STAGE_TITLES.get(stage, stage or "对话"), stage_turns)
                for stage, stage_turns in grouped.items()
            }
        for stage, content_of_stage in rendered.items():
            title_of_stage = _STAGE_TITLES.get(stage, stage or "对话")
            stage_names.append(title_of_stage)
            chat_file = f"chats/{folder}/{_safe_name(title_of_stage, 'chat')}.md"
            builder.add_text(chat_file, content_of_stage)

        solution_summary.append(
            {
                "id": solution_id,
                "title": title,
                "mode": solution.get("mode"),
                "stage": solution.get("stage"),
                "created_at": solution.get("created_at"),
                "updated_at": solution.get("updated_at"),
                "content_updated_at": solution.get("content_updated_at"),
                "documents": doc_meta,
                "chat_threads": stage_names,
            }
        )

    builder.add_text("solutions.json", _dump_json(solution_summary))

    # —— 任务记录 ——
    # utf-8-sig：带 BOM 的 UTF-8 是 Excel 打开中文 CSV 唯一不乱码的格式。
    # 不带 BOM 时中文列头会变成乱码，而「导出给运营/审核看」正是它的用途。
    buffer = io.StringIO(newline="")
    fields = [
        "id",
        "kind",
        "doc_kind",
        "chat_stage",
        "status",
        "phase",
        "solution_id",
        "created_at",
        "updated_at",
        "prompt_tokens",
        "completion_tokens",
        "error_code",
        "error_message",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    with session_scope() as session:
        jobs = AccountDataRepository(session).export_jobs(user_id)
        # 模型 → 一行 CSV：`extrasaction="ignore"` 负责丢掉 `fields` 之外的列
        # （`draft_content` 这类正文缓冲不该进导出包）。
        for job in jobs:
            writer.writerow(job.model_dump())
    builder.add_text("jobs.csv", "\ufeff" + buffer.getvalue())

    # —— 清单 ——
    builder.add_text(
        "manifest.json",
        _dump_json(
            {
                "app": "PRDwright",
                "exported_at": utc_ms_text(utc_now()),
                "exported_by": exported_by,
                "user": {"id": user_id, "username": username},
                "counts": counts,
                "notes": [
                    "本包是导出时刻的快照：此后新增或修改的内容不在其中。",
                    "不包含密码哈希、salt，也不包含任何其他账号的数据。",
                    "对话里的选择题以「请你确认」列出，✓ 表示当时选中的选项。",
                    "jobs.csv 用 UTF-8 BOM 编码，Excel 可直接打开。",
                ],
            }
        ),
    )

    payload, file_count = builder.finish()
    stamp = utc_now().strftime("%Y%m%d-%H%M")
    safe_username = _safe_name(username, f"user-{user_id}", limit=32)
    return payload, f"prdwright-{safe_username}-{stamp}.zip", file_count
