"""PRD 技能包（`skills/prd-generator`）的资源加载与输入材料格式化。

从 `document_service` 里抽出来，让**整篇生成**（document_service）与**分章节生成**
（prd_section_writer）共用同一套「技能资源定位 + 输入材料转文本」逻辑，同时避免
两个 service 互相导入形成循环依赖。

本模块只依赖标准库，不导入任何业务 service。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from backend.services.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# PRD 生成依赖的技能包目录名（仓库根/skills/<名称>/）
PRD_SKILL_NAME = "prd-generator"

# PRD 生成所需的技能资源：`键 -> 技能包内相对路径`
PRD_SKILL_ARTIFACTS: dict[str, str] = {
    "field_schema": "references/field-schema.json",
    "prd_template": "references/prd-template.md",
    "writing_rules": "references/writing-rules.md",
}

# 空对话历史的占位文案（提示词里需要一个明确的「无」而不是空白）
NO_CONVERSATION_PLACEHOLDER = "（无补充对话记录）"


def resolve_skill_dir(skill_name: str) -> Path:
    """定位技能包根目录。

    查找顺序：
    1. 环境变量 `PRDWRIGHT_SKILLS_DIR` 指向的目录下查找（部署时后端与 skills
       目录可能不在同一棵目录树里，用它显式指定 skills 根目录）。
    2. 从当前文件向上逐级查找 `<祖先目录>/skills/<skill_name>`。

    Args:
        skill_name: 技能包目录名，如 `prd-generator`。

    Returns:
        技能包根目录路径。找不到时返回按目录层级推算的默认路径，读取时会报错。
    """
    env_dir = os.environ.get("PRDWRIGHT_SKILLS_DIR")
    if env_dir:
        return Path(env_dir) / skill_name

    here = Path(__file__).resolve()
    for parent in here.parents[:6]:
        candidate = parent / "skills" / skill_name
        if candidate.is_dir():
            return candidate
    # 兜底：services/ → backend/ → src/ → backend/ → 仓库根
    return here.parents[4] / "skills" / skill_name


# 读盘缓存：`资源绝对路径 -> (文件戳, 内容)`。
# 文件戳取 `(st_mtime_ns, st_size)`：内容一改必然变其一，所以既不用每轮生成都读盘，
# 也不会像原先的 `lru_cache` 那样把旧内容锁到进程结束（债务 D10）。
# **只有读成功之后才写缓存**：资源原本缺失时会直接抛异常而非留一条空缓存，
# 文件补齐后下一次调用立刻就能读到。
_ARTIFACT_CACHE: dict[str, tuple[tuple[int, int], str]] = {}


def _artifact_stamp(path: Path) -> tuple[int, int]:
    """资源的当前文件戳：纳秒级修改时间 + 字节数。"""
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


def load_skill_artifact(skill_name: str, relative_path: str) -> str:
    """读取技能包内的资源文件（模板、规范、字段契约等）。

    缓存判定用「修改时间 + 大小」双条件而不是只有秒级 mtime：同一秒内连续改写模板时，
    大小多半也会变；两者都没变就当作没改，一次生成读一次盘的开销可以忽略。
    （注：编辑器保存的瞬间存在极小概率读到半截文件，重发一次即可，不做额外加锁。）

    Args:
        skill_name: 技能包目录名，如 `prd-generator`。
        relative_path: 资源在技能包内的相对路径，如 `references/prd-template.md`。

    Returns:
        文件文本内容（UTF-8，行尾统一为 `\\n`）。

    Raises:
        FileNotFoundError: 技能包目录或资源文件不存在。
    """
    path = resolve_skill_dir(skill_name) / relative_path
    cache_key = str(path)
    try:
        stamp = _artifact_stamp(path)
    except FileNotFoundError as exc:
        # 资源被删掉时一并清掉旧缓存，避免「文件已经没了还在拿旧内容生成」
        _ARTIFACT_CACHE.pop(cache_key, None)
        raise FileNotFoundError(
            f"技能资源缺失：{path}"
            "（可设置环境变量 PRDWRIGHT_SKILLS_DIR 指定 skills 根目录）"
        ) from exc

    cached = _ARTIFACT_CACHE.get(cache_key)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    text = path.read_text(encoding="utf-8")
    _ARTIFACT_CACHE[cache_key] = (stamp, text)
    return text


def clear_artifact_cache() -> None:
    """清空资源读盘缓存。

    正常路径不需要调用它（改文件会让文件戳失效）；测试里用来保证下一次读取真的走盘，
    避免同一份 fixture 在用例之间互相污染。
    """
    _ARTIFACT_CACHE.clear()


def missing_skill_assets(skill_name: str = PRD_SKILL_NAME) -> list[str]:
    """返回当前读不到的必需资源的相对路径（齐全时为空列表）。

    自检只看「文件在不在」，不读内容：读内容是加载阶段的事，混在一起会让
    启动自检变得很重，也让「缺失」和「损坏」两种问题分不清。
    """
    root = resolve_skill_dir(skill_name)
    return [
        relative_path
        for relative_path in PRD_SKILL_ARTIFACTS.values()
        if not (root / relative_path).is_file()
    ]


def skill_version(skill_name: str = PRD_SKILL_NAME) -> str:
    """读技能包 `skill.yaml` 声明的版本号；读不到时返回 `"unknown"`。

    刻意用正则而不是引入 PyYAML：这里只需要一个标量，为它加一层依赖不划算。
    """
    try:
        raw = load_skill_artifact(skill_name, "skill.yaml")
    except FileNotFoundError:
        return "unknown"
    match = re.search(r"^version:\s*([^\s#]+)", raw, re.MULTILINE)
    return match.group(1) if match else "unknown"


def skill_fingerprint(skill_name: str = PRD_SKILL_NAME) -> str:
    """三份资源合成的整体指纹：`sha256(版本 + 各资源的路径与内容)[:12]`。

    给回执用：两份 PRD 的生成指纹不同，就一定不是同一套模板写的，不用去猜。
    它同时也是 `job_fingerprint.PRD_TEMPLATE_VERSION` 的自动化版本——那个常量要求
    人工 +1，容易忘；这里的指纹不会因为忘了改版本号而失效。

    Raises:
        FileNotFoundError: 任一资源缺失时抛出（调用方请先做 `missing_skill_assets`）。
    """
    digest = hashlib.sha256()
    digest.update(skill_version(skill_name).encode("utf-8"))
    for key in sorted(PRD_SKILL_ARTIFACTS):
        relative_path = PRD_SKILL_ARTIFACTS[key]
        digest.update(relative_path.encode("utf-8"))
        digest.update(load_skill_artifact(skill_name, relative_path).encode("utf-8"))
    return digest.hexdigest()[:12]


def describe_skill(skill_name: str = PRD_SKILL_NAME) -> dict[str, str] | None:
    """把技能包的身份信息打包成 `run_summary.injected_skills` 的一个条目。

    资源不齐全时返回 `None`：此时生成会降级到内置提示词，而「这次到底有没有用上
    技能包」必须能在回执上看得见（债务 D8），因此不返回残缺条目也不返回占位值。
    """
    if missing_skill_assets(skill_name):
        return None
    return {
        "name": skill_name,
        "version": skill_version(skill_name),
        "fingerprint": skill_fingerprint(skill_name),
    }


def ensure_skill_assets(skill_name: str = PRD_SKILL_NAME, *, strict: bool = False) -> list[str]:
    """启动自检：缺了哪些必需资源。`strict` 为真时缺失直接 fail-fast。

    放在应用 `lifespan` 里跑一遍，部署漏打包 `skills/` 目录就能在**起服务的时候**
    炸出来，而不是等某次生成悄悄退回内置提示词、PRD 风格变了都没人知道（债务 D8）。

    Args:
        skill_name: 技能包目录名。
        strict: 为真时任一资源缺失都抛错中止启动。

    Returns:
        缺失资源的相对路径列表（齐全时为空列表）。

    Raises:
        RuntimeError: `strict` 为真且存在缺失资源。
    """
    missing = missing_skill_assets(skill_name)
    if missing and strict:
        raise RuntimeError(
            f"技能包 {skill_name} 资源缺失：{missing}。"
            f"已解析的技能包目录为 {resolve_skill_dir(skill_name)}；"
            "可用环境变量 PRDWRIGHT_SKILLS_DIR 指定 skills 根目录，"
            "或在部署产物中打包该目录。不想让启动失败请把 PRDWRIGHT_SKILLS_STRICT 设为 false。"
        )
    return missing


def load_prd_skill_artifacts() -> dict[str, str] | None:
    """加载 PRD 生成所需的三份技能资源。

    Returns:
        键为 `field_schema` / `prd_template` / `writing_rules` 的资源字典；
        任一资源缺失时返回 `None`，由调用方降级到内置提示词。
    """
    missing = missing_skill_assets(PRD_SKILL_NAME)
    if missing:
        logger.warning(
            "PRD 技能资源缺失（%s），本次改用内置提示词生成。"
            "生产环境应把 PRDWRIGHT_SKILLS_STRICT 设为 true，让缺失在启动时暴露",
            "、".join(missing),
        )
        return None
    return {
        key: load_skill_artifact(PRD_SKILL_NAME, relative_path)
        for key, relative_path in PRD_SKILL_ARTIFACTS.items()
    }


def format_summary_as_requirements(summary: dict[str, Any]) -> str:
    """把结构化需求摘要转成「需求文本」，供 PRD 提示词使用。

    这里刻意保留 schema 原貌（缩进 JSON）而不改写成散文：提示词里已经带了
    `field-schema.json` 字段契约，模型可以据此直接逐字段校验与归一化；
    转成散文反而会丢掉数组结构（MVP 功能逐行、页面清单、范围内外等）。

    Args:
        summary: 结构化需求摘要。

    Returns:
        形如「说明 + JSON 代码块」的需求文本。
    """
    body = json.dumps(summary, ensure_ascii=False, indent=2)
    return (
        "以下为结构化需求摘要（字段与「输入字段契约」一致，数组为逐项录入结果）：\n\n"
        "```json\n"
        f"{body}\n"
        "```"
    )


def format_freeform_requirements(requirements: str, product_name: str = "") -> str:
    """把**自由文本**需求（PRD 快捷入口的一段话描述）转成提示词里的需求文本。

    与 `format_summary_as_requirements` 并列，区别在于输入形态：那条路径输入的是
    与 schema 对齐的 JSON，这条路径输入的是用户随手写下的一段话。提示词里同时带着
    `field-schema.json` 字段契约，若不明说，模型容易误以为输入是结构化摘要、进而
    把契约里的字段当成「用户已提供」；因此这里显式声明输入形态，并重申缺失信息按
    「缺失信息处理」标注「待确认」。

    Args:
        requirements: 用户输入的自由文本需求。
        product_name: 用户在快捷入口单独填写的产品名称，可为空。

    Returns:
        形如「输入形态说明 + 产品名（可选）+ 需求正文 + 缺失信息提醒」的需求文本。
    """
    name = product_name.strip()
    name_line = f"用户填写的产品名称：{name}\n\n" if name else ""
    return (
        "以下为用户用自然语言直接描述的需求（**不是**结构化摘要，也不保证字段齐全）：\n\n"
        f"{name_line}"
        f"{(requirements or '').strip()}\n\n"
        "上述描述中未出现、而「输入字段契约」里列为必填的信息，一律按写作规范的"
        "「缺失信息处理」标注「待确认」，禁止自行补全或编造。"
    )


def format_conversation(history: list[dict[str, Any]] | None) -> str:
    """把对话历史（`{role, content}` 列表）拼成易读的 Markdown 文本。

    Args:
        history: 前端回传的对话消息，元素形如 `{"role": ..., "content": ...}`。

    Returns:
        形如 `**用户**：...` / `**AI**：...` 的多行文本；空历史返回占位文案。
    """
    if not history:
        return NO_CONVERSATION_PLACEHOLDER

    lines: list[str] = []
    for item in history:
        role = str(item.get("role", "")).strip().lower()
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        speaker = "用户" if role == "user" else "AI" if role == "assistant" else role or "系统"
        lines.append(f"**{speaker}**：{content}")
    return "\n\n".join(lines) if lines else NO_CONVERSATION_PLACEHOLDER


def build_prd_system_prompt(writing_rules: str) -> str:
    """把默认人设与技能包的写作规范拼成 PRD 生成的系统提示词。

    整篇生成与分章节生成必须用同一套人设与规范，否则两条路径产出的文档风格会不一致，
    因此把拼接逻辑收口在这里，两边共用。

    Args:
        writing_rules: `skills/prd-generator/references/writing-rules.md` 的全文。

    Returns:
        系统提示词：默认人设 + 分隔线 + 写作规范。
    """
    return (
        f"{SYSTEM_PROMPT}\n\n---\n\n"
        "# PRD 写作规范（生成时必须逐条遵守）\n\n"
        f"{writing_rules}"
    )

