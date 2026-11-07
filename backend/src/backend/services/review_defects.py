"""审核的「缺陷」口径：类别、固定严重度、清单注入（ADR 0003）。

审核只报**缺陷**——可验证为错的地方，共六类。「缺什么、该怎么定」是**未决**，归待确认
清单，进不了审核结论。这里收着这条分工的可执行部分：

1. **类别 → 严重度**：编造与矛盾 = `high`（生成时触发一次自动改写），其余 = `medium`
   （只报不改）。严重度**由类别决定**，模型自报的那个一律按类别覆写——同一类问题这次
   `high` 下次 `medium`，改写门槛就会随模型心情漂移。
2. **类别表只有一个生成点**：提示词里的五类判据由 `render_category_rules()` 渲染，
   第六类与清单由 `render_pending_section()` 在同一份提示词的 `{pending_section}` 里给出。
   PRD 的单章审核（`prd_section_writer`）与整篇审核（`document_review`）都从这里取；
   各写一份的下场是「同一份正文在 PRD 侧判 medium、在接口文档侧判 high」。
3. **两个时点两套口径**：生成过程只看前五类（那时清单还没抽出来）；重审与修复后的重审
   才看第六类「清单漏登记」，并且必须把当前版本的清单整份注入提示词。
4. **旧结论不追溯**：库里已存的结论没有类别，读出来 `category=None`——界面归「未分类」，
   不推断、不重分类。容错**只作用于读历史**，见 `normalize_issue`。
"""

from __future__ import annotations

import re
from typing import Any

from backend.services.pending_items import format_pending_items

# 类别 → 固定严重度。顺序即界面分组顺序（前五类属于生成过程，第六类只在重审时开）。
CATEGORY_SEVERITY: dict[str, str] = {
    "fabrication": "high",
    "contradiction": "high",
    "structure": "medium",
    "vague": "medium",
    "unlabeled_inference": "medium",
    "missing_pending": "medium",
}

# 类别 → 中文标签（提示词与日志共用的那一份；界面上那份在前端 `utils/review.ts`，
# 有对拍用例守着逐项相等）
CATEGORY_LABELS: dict[str, str] = {
    "fabrication": "编造",
    "contradiction": "矛盾",
    "structure": "结构不符",
    "vague": "空泛",
    "unlabeled_inference": "推断未标注",
    "missing_pending": "清单漏登记",
}

# 类别 → 判据（提示词里那一行「为什么这一类算缺陷」）。三份产物共用同一份措辞：
# 上游材料在 PRD 侧是「需求输入 + 已定稿章节」，在接口文档 / 提示词侧是上游 PRD 与
# 文本本身，措辞因此只写到「上游材料」为止。
CATEGORY_CRITERIA: dict[str, str] = {
    "fabrication": (
        "写了具体的数据、时间节点、技术栈、接口或字段名、功能名或第三方服务，"
        "而上游材料里检索不到出处。"
    ),
    "contradiction": (
        "与上游材料冲突，或与已定稿的上游章节口径冲突（产品名、编号、命名、路径、范围）。"
    ),
    "structure": (
        "标题层级、表格列、条目编号与产出规范 / 写作规范或模板不一致。"
    ),
    "vague": (
        "出现无法直接开工、也无法验证的描述（「用户管理」「数据看板」这类只给名词的写法）。"
    ),
    "unlabeled_inference": (
        "**只判形式**——属于推断的内容没有标「（建议）」，或没有给出一句依据。"
        "依据本身够不够、偏不偏弱**不判**：那是主观，也是同一件事被反复报的来源。"
    ),
}

# 只有在重审时开的那一类：生成链路若报了它，服务端直接丢掉（不许模型越过时点口径）。
MISSING_PENDING = "missing_pending"

# 写法容差：中文标签 + 下划线 / 连字符 / 空格 / 大小写变体都归一到六类的英文键。
# 类别键名不是软闸——认得出类别，才抬得起档、也才丢得掉不该报的那一类。
_CATEGORY_ALIASES: dict[str, str] = {
    label: key for key, label in CATEGORY_LABELS.items()
}


def _canonical_spelling(raw: str) -> str:
    """把英文标识的写法差异抹平：大小写、首尾空白、下划线 / 连字符 / 空格统一成 `_`。"""
    lowered = raw.strip().lower()
    return re.sub(r"_+", "_", re.sub(r"[\s\-]+", "_", lowered)).strip("_")


def normalize_category(raw: Any) -> str | None:
    """把模型给的类别规范成六类之一；缺失或不认识的值返回 `None`。

    返回 `None` 表示「这一条没有类别」——旧结论形状，不是「猜一个类别」。
    认得出的是别名（中文标签、`missing-pending`、`Missing Pending` 之类），
    一律写回**规范键**：下游按类别定档位、按时点丢弃都只认这六个键。

    Args:
        raw: 模型给的类别值（任意类型；不是字符串时按缺失处理）。

    Returns:
        六类之一的规范键；缺失或不认识时为 `None`。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[text]
    key = _canonical_spelling(text)
    return key if key in CATEGORY_SEVERITY else None


def normalize_issue(
    item: dict[str, Any], *, allow_missing_pending: bool
) -> dict[str, Any] | None:
    """把一条问题归一成结论里的形状；该丢弃时返回 `None`。

    判据是 **`"category" in item`**：有这一格的是**新输出**，没有的是**旧结论形状**。
    两条路必须分开，否则「旧结论容错」会变成「新输出也能绕过类别定档」：

    - **没有 `category` 键**（旧结论形状，ADR 0003 决策 7 的容错范围）：保留条目、
      保留模型自报的严重度，只做大小写 / 空白规整；缺了严重度按 `medium`（与 PRD
      的 `section_issue` 同一口径，见 A4）。旧版解析从来没有按严重度筛过条目，
      新口径不该顺手把历史形状收紧（那等于替用户改判）。
    - **有 `category` 键但归不到六类**：`category=None`，**严重度强制 `medium`**，
      绝不采信模型自报的 `high`。理由：新输出认不出的类别按最低可改写档处理，
      宁可只报不改。
    - 类别认得出 → **严重度按类别覆写**（模型写什么都不算数）。
    - 生成过程中报了 `missing_pending`（任何能认出来的写法）→ 丢掉：那时清单还没抽出来，
      该判定不可能成立。

    Args:
        item: 模型给出的一条问题。
        allow_missing_pending: 这一时点是否允许判「清单漏登记」。

    Returns:
        归一后的问题；`None` 表示这条不进结论。
    """
    issue = dict(item)
    declares_category = "category" in item
    category = normalize_category(issue.get("category"))
    if category == MISSING_PENDING and not allow_missing_pending:
        return None
    if category is not None:
        issue["severity"] = CATEGORY_SEVERITY[category]
    elif not declares_category:
        # 旧结论形状：只做大小写与空白的规整，不筛条目、不改判；空档位补 medium
        issue["severity"] = str(issue.get("severity") or "").strip().lower() or "medium"
    else:
        # 新输出认不出的类别按最低可改写档处理，宁可只报不改
        issue["severity"] = "medium"
    issue["category"] = category
    return issue


def normalize_issues(
    items: list[dict[str, Any]], *, allow_missing_pending: bool
) -> list[dict[str, Any]]:
    """逐条归一，丢掉不该进结论的那些（见 `normalize_issue`）。"""
    normalized: list[dict[str, Any]] = []
    for item in items:
        issue = normalize_issue(item, allow_missing_pending=allow_missing_pending)
        if issue is not None:
            normalized.append(issue)
    return normalized


def render_category_rules() -> str:
    """提示词里的**五类判据**——三份产物共用的唯一生成点。

    第六类「清单漏登记」不在这里：它只在重审时开，由 `render_pending_section` 连着
    当前版本的清单一起给出（同一个 `{pending_section}` 开关，见模块说明）。

    Returns:
        编号列表形式的判据文本（第 1~5 类）。
    """
    lines: list[str] = []
    for index, (category, severity) in enumerate(CATEGORY_SEVERITY.items(), start=1):
        if category == MISSING_PENDING:
            continue
        label = CATEGORY_LABELS[category]
        criteria = CATEGORY_CRITERIA[category]
        lines.append(f"{index}. **{label}**（{severity}，`category` 写 `{category}`）：{criteria}")
    return "\n".join(lines)


def render_pending_section(items: list[dict[str, Any]] | None) -> str:
    """重审提示词里那一格：第六类判据 + 当前版本的待确认清单。

    取空列表时**仍然给出第六类**——清单为空恰恰最容易漏登记，不能因为「没有清单」就
    把这类的判定关掉。生成时调用方传的是空串（不调用本函数），因此那一态里既没有这一类，
    也没有清单。

    判据里要求**定位与缺项类别**（AC2）：只说「漏了」不够，得说清「在哪一节、缺的是
    哪一类项」，否则用户拿到的是一条无法核对的条目。

    Args:
        items: 当前版本的待确认清单（后端结构）。

    Returns:
        可直接替换提示词里 `{pending_section}` 的文本。
    """
    rules = (
        "6. **清单漏登记**（medium，`category` 写 `missing_pending`）：正文里有需要用户拍板、"
        "却**没有登记**在下面这份清单里的项。已经登记过的项不算缺陷，不要重复报。"
        "**必须写清定位与缺项**：`where` 写问题所在的小节标题（逐字取自上面给出的章节清单），"
        "`detail` 写清「缺的是哪一类」（登录方式、鉴权方式、验收标准之类）且不超过 40 字；"
        "`where` 与 `detail` 里至少有一处要让读者知道「在哪一节、缺的是哪一类」。"
    )
    if not items:
        body = (
            "本版没有登记任何待确认项。正文里若有需要用户拍板、却无处可去的项，"
            "仍按「清单漏登记」记。"
        )
    else:
        body = format_pending_items(list(items))
    return f"{rules}\n\n### 当前版本的待确认清单（判定「清单漏登记」的唯一依据）\n\n{body}"


def render_pending_list(items: list[dict[str, Any]] | None) -> str:
    """把当前版本的清单摊平成一段文本（修复提示词用：已登记过的不必再登记）。

    Args:
        items: 当前版本的待确认清单（后端结构）。

    Returns:
        编号列表；空清单时给一句明确的「本版没有」。
    """
    if not items:
        return "本版没有登记任何待确认项。"
    return format_pending_items(list(items))


def defects_count_text(issues: list[dict[str, Any]]) -> str:
    """按类别计数的一句话：`编造 1 · 结构不符 2`；没有类别的归「未分类」。

    这是**界面之外**唯一需要自己数一遍类别的地方（PRD 的汇总结论）：界面自己有一份
    分组计数，结论里这句话则是落库、要被读出来的那句总结。顺序即类别表顺序，未分类垫底。

    Args:
        issues: 结论里的逐条问题。

    Returns:
        形如 `编造 1 · 结构不符 2` 的文本；没有问题时为 `""`。
    """
    counts: dict[str | None, int] = {}
    for issue in issues:
        key = normalize_category(issue.get("category")) if isinstance(issue, dict) else None
        counts[key] = counts.get(key, 0) + 1
    parts: list[str] = []
    for key in [*CATEGORY_SEVERITY, None]:
        number = counts.get(key, 0)
        if not number:
            continue
        label = CATEGORY_LABELS[key] if key is not None else "未分类"
        parts.append(f"{label} {number}")
    return " · ".join(parts)


def finalize_verdict(
    verdict: str, *, issues: list[dict[str, Any]], instruction: str, dropped: int
) -> tuple[str, str]:
    """判定词与修改指令的收尾：**条目被时点口径丢光**时，不算「有问题」。

    这是 AC3 的实现，只服务一件事：生成链路里模型偶尔会自报一条「清单漏登记」，它被
    服务端丢掉之后，若判定仍是 `revise`、指令还留着，改写门槛（`high` 才动笔那条路
    看 `issues`；没有 `issues` 时按「有指令就改」放行）就会为一条**不存在的缺陷**
    白改一次。指令是照着那些缺陷写的，缺陷没了，指令也该一起清掉。

    **别扩大它**：只有「确实丢了条目（`dropped > 0`）且一条都没剩」才降档清指令。
    还有条目在、或者压根没丢过，指令与判定都原样保留——替用户清掉一条与丢弃无关的
    指令，等于悄悄改掉审核结论。

    Args:
        verdict: 解析出的判定词。
        issues: 归一后仍保留的问题。
        instruction: 解析出的修改指令。
        dropped: 被过滤掉的条目数。

    Returns:
        `(判定, 指令)` 二元组。
    """
    if dropped and not issues and verdict == "revise":
        return "pass", ""
    return verdict, instruction


def issues_text(
    issues: list[dict[str, Any]], *, empty_text: str = "", clip: Any = None
) -> str:
    """把结论里的问题排成给改写提示词用的编号列表：`[severity] where：detail`。

    类别**不进这里**：改写那一步要的是「改哪儿、改什么」，类别是给界面与结论用的；
    把它塞进改写提示词只会多一层翻译，还让两个 `issues_text` 的调用方各写一遍标签。
    空清单的占位句与 detail 的截断长度由调用方给（两条产物链的口径本来就不同）。

    Args:
        issues: 归一后的问题列表。
        empty_text: 没有问题时返回的占位句（各调用方自己的措辞）。
        clip: 可选的截断函数（保持调用方原有的 `detail` 长度口径）。

    Returns:
        编号列表；没有问题时为 `empty_text`。
    """
    if not issues:
        return empty_text
    lines: list[str] = []
    for index, issue in enumerate(issues, start=1):
        severity = str(issue.get("severity") or "medium")
        detail = str(issue.get("detail") or "")
        if callable(clip):
            detail = str(clip(detail))
        where = str(issue.get("where") or "").strip()
        prefix = f"[{severity}]"
        if where:
            prefix += f" {where}"
        lines.append(f"{index}. {prefix}：{detail}")
    return "\n".join(lines)
