"""终检守卫：文档写出的路径可见、全仓无悬空引用、收口结论落在指定落点（票 #41 / #44）。

接缝是「工作树里的 Markdown 文档与 nine-layer 旧路径字面量」：从文档自身的
相对位置解析链接，从仓库根解析反引号里的全路径，逐条要求文件真的存在。
不用 git / 子进程，纯文件系统读，离线可跑、xdist 各 worker 结果一致。

#44 追加的守卫把「维护者打开仓库能读到什么结论」也当成公开接缝：读现状架构文档的
「取舍」节、路线图的票序、约定的正向条文、时间线真源的失真表与词表的概念定义，
断言票面要求的结论确实落在这些落点上。期望值来自票面验收标准，不扫源码、
不数落点、不查标识符词频。

路径扫描白名单（#33 报告，按类别排除，不是逐个路径硬编码放行）：
- 运行时生成物：`backend/secrets/master.key` 由 `scripts/keygen/generate_keys.py`
  生成，`.gitignore` 明确排除 `backend/secrets/`，检出的工作树里本就不该有；
  `deploy/.env` 同为部署时从样例复制出来的本地文件，`.gitignore` 排除 `.env`。
- 文档内简写：`README.md` / `SKILL.md` 这类只有文件名、没有目录前缀的写法，
  指的是当前文档所在目录或技能包目录内的文件，不是完整仓库路径。
- 通配 / 花括号写法：`frontend/*.html`、`frontend/src/**`、
  `scripts/deploy/{backup,restore,release,smoke}.py` 是模式或明确标注「从未落盘」
  的历史叙述，不能按单一路径去 `Test-Path`。

明确标注「已作废 / 从未落盘 / 只在 git 历史里」的历史叙述不算悬空引用；
本守卫抓的是**仍在正向使用**的 `v[1-9]/...` 字面量。
"""

from __future__ import annotations

import re
from pathlib import Path

# `backend/tests/` → 仓库根。
_REPO_ROOT = Path(__file__).resolve().parents[2]

# 反引号里以这些目录开头才算「仓库路径」；纯文件名 / 命令 / URL 不进扫描。
_PATH_PREFIX = re.compile(
    r"^(?:\.github|docs|backend|deploy|skills|scripts|frontend|frontend_admin|\.\.?)/"
)
_BACKTICK_PATH = re.compile(r"`((?:\.\.?/|(?:\.github|docs|backend|deploy|skills|scripts|frontend|frontend_admin)/)[^`]+)`")
_MD_LINK = re.compile(r"\]\(([^)]+)\)")
# 九层目录已删；再出现这种字面量就是指向不存在的落点。
_DANGLING_LAYER = re.compile(r"v[1-9]/(?:ARCHITECTURE|README|roadmap)\.md")
# 旧口径：只保留明确标注作废的历史叙述。守卫自身的清单行也是「引用」，
# 因此扫描时跳过本文件（它不承载架构结论）。
_OLD_PHRASES = (
    "写" + "在该层",
    "不再" + "单独归档",
    "一层" + "一套架构文档",
    "新旧" + "并存",
    "决策与否决理由" + "不再" + "单独归档",
)
_HISTORY_MARKERS = ("已作废", "旧判断", "旧口径", "从未落盘", "只在 git 历史里")

# 白名单：按类别，不是逐条放行。键是「类别」，值是可解析出的路径片段。
# `backend/secrets/` 是运行时生成物的父目录，同样不随检出落盘。
_RUNTIME_GENERATED = frozenset({"backend/secrets/master.key", "backend/secrets/", "deploy/.env"})
_DOC_SHORTHANDS = frozenset({"README.md", "SKILL.md"})
_GLOB_MARKERS = ("*", "{", "}", "<")


def _markdown_files() -> list[Path]:
    """从工作树实读 Markdown（不依赖 git 索引，新写的文件也算）。"""
    # 嵌套 worktree 是别的分支的工作副本，停在旧提交、会带旧文案；
    # 终检只判主工作树，别把 agent 的临时副本算进全仓结论。
    skip = {".venv", "node_modules", "dist", ".git", ".agents"}
    return sorted(
        path
        for path in _REPO_ROOT.rglob("*.md")
        if not any(part in skip for part in path.relative_to(_REPO_ROOT).parts[:-1])
    )


def _clean_token(raw: str) -> str:
    """把反引号内容截成可解析的路径：去掉 `§x`、`:行号`、`::符号` 等后缀。"""
    token = raw.split()[0] if raw.split() else ""
    token = re.sub(r":\d.*$", "", token)
    token = re.sub(r"::.*$", "", token)
    return token.rstrip(".,;")


def _resolve(path: Path, token: str) -> Path:
    """相对路径按文档所在目录解析，其余按仓库根解析。"""
    if token.startswith(("./", "../")):
        return (path.parent / token).resolve()
    return (_REPO_ROOT / token).resolve()


def test_markdown_links_point_at_existing_files() -> None:
    """Markdown 链接的目标逐条可见（相对文档目录或仓库根）。"""
    missing: list[str] = []
    for path in _markdown_files():
        for match in _MD_LINK.finditer(path.read_text(encoding="utf-8")):
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:", "#", "/")):
                continue
            if any(marker in target for marker in _GLOB_MARKERS):
                continue
            target = target.split("#", 1)[0].strip()
            if not target:
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists() and not (_REPO_ROOT / target).resolve().exists():
                missing.append(f"{path.relative_to(_REPO_ROOT)} -> {target}")
    assert missing == [], "以下 Markdown 链接指向不存在的落点：\n" + "\n".join(missing)


def test_backtick_repo_paths_exist_or_are_whitelisted() -> None:
    """反引号里的仓库路径逐条可见；白名单按类别排除并写明依据。"""
    missing: list[str] = []
    seen: set[str] = set()
    for path in _markdown_files():
        for match in _BACKTICK_PATH.finditer(path.read_text(encoding="utf-8")):
            token = _clean_token(match.group(1))
            if not token or not _PATH_PREFIX.match(token):
                continue
            if token in _RUNTIME_GENERATED or token in _DOC_SHORTHANDS:
                continue
            if any(marker in token for marker in _GLOB_MARKERS):
                continue
            if token in seen:
                continue
            seen.add(token)
            if not _resolve(path, token).exists():
                missing.append(f"{path.relative_to(_REPO_ROOT)} -> {token}")
    assert missing == [], "以下写出的仓库路径看不见（且不属于白名单类别）：\n" + "\n".join(missing)


def test_no_dangling_layer_references_or_live_old_wording() -> None:
    """全仓无指向九层目录的悬空引用，旧口径只剩明确标注作废的历史叙述。"""
    dangling: list[str] = []
    stale: list[str] = []
    # 嵌套 worktree 是别的分支的工作副本，停在旧提交、会带旧文案；
    # 终检只判主工作树，别把 agent 的临时副本算进全仓结论。
    skip = {".venv", "node_modules", "dist", ".git", ".agents"}
    suffixes = {".md", ".py", ".ts", ".json", ".yaml", ".yml", ".toml", ".sh", ".txt"}
    for path in sorted(_REPO_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        rel_parts = path.relative_to(_REPO_ROOT).parts
        if any(part in skip for part in rel_parts[:-1]):
            continue
        if path.name == "test_closeout_docs.py":
            continue
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(_REPO_ROOT))
        for number, line in enumerate(text.splitlines(), start=1):
            if _DANGLING_LAYER.search(line):
                dangling.append(f"{rel}:{number} {line.strip()}")
            for phrase in _OLD_PHRASES:
                if phrase in line and not any(marker in line for marker in _HISTORY_MARKERS):
                    stale.append(f"{rel}:{number} 旧口径「{phrase}」未标注作废")
    assert dangling == [], "以下行仍指向已删除的九层文档：\n" + "\n".join(dangling)
    assert stale == [], "以下旧口径仍在正向使用：\n" + "\n".join(stale)


def test_whitelist_categories_have_documented_evidence() -> None:
    """白名单不是随口放行：依据（gitignore / 技能包目录）必须真的存在。"""
    gitignore = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "backend/secrets/" in gitignore, "主密钥运行时生成物没有 .gitignore 依据"
    assert re.search(r"^\.env$", gitignore, re.MULTILINE), "部署 .env 没有 .gitignore 依据"
    for skill in ("prd-generator", "api-docs-generator"):
        assert (_REPO_ROOT / "skills" / skill / "SKILL.md").is_file(), (
            f"技能包 {skill} 的文档内简写没有落点"
        )


def _doc(rel: str) -> str:
    """读工作树里的一份文档（相对仓库根），不经过 git 索引。"""
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    """取两个标题之间的正文；标题缺失时返回空串，交给断言报出可读的落点。"""
    if start not in text or end not in text:
        return ""
    head = text.index(start)
    return text[head : text.index(end, head + len(start))]


def _from(text: str, start: str) -> str:
    """取从某个标题到文末的正文。"""
    return text[text.index(start) :] if start in text else ""


def test_entry_registry_conclusions_are_in_current_docs() -> None:
    """#44：维护者打开仓库，登记表结论与校正数字都能在指定落点读到。

    期望值来自票面验收标准原文，与实现无关；只判断「文档对外给出什么结论」，
    不扫源码、不数落点、不查标识符词频。
    """
    missing: list[str] = []

    def require(label: str, section: str, *anchors: str) -> None:
        for anchor in anchors:
            if anchor not in section:
                missing.append(f"{label} 缺「{anchor}」")

    # 现状架构文档：结论写在「取舍」节新增的小节里，且按目标 → 范围 → 设计 → 契约 → 取舍正向写。
    architecture = _doc("docs/architecture/ARCHITECTURE.md")
    tradeoffs = _between(architecture, "## 5. 取舍", "## 6.")
    registry = _between(tradeoffs, "### 5.8", "### 5.9")
    require(
        "ARCHITECTURE.md §5.8",
        registry,
        "一张登记表",
        "一行一个入口",
        "七类资料",
        "只收随入口模式变化的事实",
        "14 个视图状态",
        "进度格",
        "后端",
        "界面交互",
        "本期不做",
        "**目标**",
        "**范围**",
        "**设计**",
        "**契约**",
        "**取舍**",
    )
    positive_order = ["**目标**", "**范围**", "**设计**", "**契约**", "**取舍**"]
    positions = [registry.find(marker) for marker in positive_order]
    if any(pos < 0 for pos in positions) or positions != sorted(positions):
        missing.append("ARCHITECTURE.md §5.8 未按「目标 → 范围 → 设计 → 契约 → 取舍」正向写")
    for stale in ("盘点", "缺口", "约 20 处"):
        if stale in registry:
            missing.append(f"ARCHITECTURE.md §5.8 仍在反向写旧口径「{stale}」")
    require("ARCHITECTURE.md §5", tradeoffs, "### 5.9 本次文档重组的决策")

    # 路线图：本轮六张票（四张代码票 + 文档票 + 终检票）的票序、落点与验收总表齐备。
    roadmap = _doc("docs/architecture/roadmap.md")
    require(
        "roadmap.md §1",
        _between(roadmap, "## 1. 票序与依赖", "## 2."),
        "#44 → #45 → #46 → #47 → #48 → #49",
        "#45 依赖 #44",
        "#49 依赖 #48",
    )
    landing = _between(roadmap, "## 2. 各票落点与验收方式", "## 3.")
    for ticket in ("**#44**", "**#45**", "**#46**", "**#47**", "**#48**", "**#49**"):
        require("roadmap.md §2", landing, ticket)
    require(
        "roadmap.md §5",
        _from(roadmap, "## 5. 验收总表"),
        "#44",
        "#45",
        "#46",
        "#47",
        "#48",
        "#49",
    )

    # 约定：人工同步清单整段退役，正向条文改描述登记表，数字按实测写。
    conventions = _doc("docs/agents/conventions.md")
    live = (
        _between(conventions, "## 1. 通用纪律", "## 2.")
        + _between(conventions, "## 6. 前端与状态机", "## 7.")
        + _between(conventions, "## 7. 持久化与版本链", "## 8.")
    )
    require("conventions.md §1/§6/§7", live, "登记表", "154 行 / 23 文件", "151 行 / 13 文件", "至少 8 处")
    retired_markers = ("退役", "作废", "旧口径", "旧文", "更正为")
    live_lines = [line for line in live.splitlines() if not any(m in line for m in retired_markers)]
    for stale in ("约 20 处", "170 个文件:行号落点", "reset / watch 四处"):
        offenders = [line for line in live_lines if stale in line]
        if offenders:
            missing.append(f"conventions.md 正向条文仍在用旧口径「{stale}」：{offenders[0][:60]}")
    require(
        "conventions.md 已失效附录",
        _from(conventions, "## 已失效（勿再引用）"),
        "登记表",
        "154 行 / 23 文件",
    )

    # 时间线真源：失真表补上四条已复核的校正与本轮实测数字。
    drift = _between(
        _doc("docs/architecture/README.md"),
        "## 4. 已确认的失真",
        "## 5. 怎么复核这里的一切",
    )
    require(
        "README.md §4 失真表",
        drift,
        "goTo",
        "clarifyTarget",
        "四处",
        "约 20 处",
        "未选择",
        "154 行 / 23 文件",
        "151 行 / 13 文件",
        "33 files changed, 1237 insertions(+), 192 deletions(-)",
        "至少 8 处",
    )

    # 词表：只装产品概念——入口模式可为「未选择」，且不写实现（登记表）与同步处数。
    glossary = _doc("GLOSSARY.md")
    states = _between(glossary, "## 入口与状态", "## 任务与运行态")
    require("GLOSSARY.md §入口与状态", states, "入口模式", "未选择")
    for stale in ("四处同步", "登记表"):
        if stale in glossary:
            missing.append(f"GLOSSARY.md 混进了实现口径「{stale}」")

    assert missing == [], "以下收口结论还没写进现状文档：\n" + "\n".join(missing)
