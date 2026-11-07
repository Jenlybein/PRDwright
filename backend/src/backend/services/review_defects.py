"""审核的「缺陷」口径：类别、固定严重度、清单注入（ADR 0003）。

审核只报**缺陷**——可验证为错的地方，共六类。「缺什么、该怎么定」是**未决**，归待确认
清单，进不了审核结论。这里收着这条分工的三个可执行部分：

1. **类别 → 严重度**：编造与矛盾 = `high`（生成时触发一次自动改写），其余 = `medium`
   （只报不改）。严重度**由类别决定**，模型自报的那个一律按类别覆写——同一类问题这次
   `high` 下次 `medium`，改写门槛就会随模型心情漂移。
2. **两个时点两套口径**：生成过程只看前五类（那时清单还没抽出来）；重审与修复后的重审
   才看第六类「清单漏登记」，并且必须把当前版本的清单整份注入提示词。因此提示词里那一格
   是**占位符**（`{pending_section}`）：生成时填空串，重审时填「第六类 + 清单」。
3. **旧结论不追溯**：库里已存的结论没有类别，读出来 `category=None`——界面归「未分类」，
   不推断、不重分类。这里不碰历史数据，只在**解析新结论**时归一。

为什么不把这段塞进 `document_review`：PRD 的单章审核（`prd_section_writer`）与整篇审核
（`document_review`）是两条链路，但类别集、严重度映射、两时点口径必须是**同一份**——
各写一份的下场是「同一份正文在 PRD 侧判 medium、在接口文档侧判 high」。
"""

from __future__ import annotations

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

# 类别 → 中文标签（提示词与日志共用的那一份；界面上的映射在前端自己那份）
CATEGORY_LABELS: dict[str, str] = {
    "fabrication": "编造",
    "contradiction": "矛盾",
    "structure": "结构不符",
    "vague": "空泛",
    "unlabeled_inference": "推断未标注",
    "missing_pending": "清单漏登记",
}

# 只有在重审时开的那一类：生成链路若报了它，服务端直接丢掉（不许模型越过时点口径）。
MISSING_PENDING = "missing_pending"


def normalize_category(raw: Any) -> str | None:
    """把模型给的类别规范成六类之一；缺失或不认识的值返回 `None`。

    返回 `None` 表示「这一条没有类别」——旧结论形状，不是「猜一个类别」。
    """
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in CATEGORY_SEVERITY:
            return value
    return None


def normalize_issue(
    item: dict[str, Any], *, allow_missing_pending: bool
) -> dict[str, Any] | None:
    """把一条问题归一成结论里的形状；该丢弃时返回 `None`。

    规则：
    - 类别合法 → **严重度按类别覆写**（模型写什么都不算数）；
    - 类别非法/缺失 → 记为 `None`（旧结论形状）：这条**照旧留着**、严重度保留模型自报的
      值——旧版解析从来没有按严重度筛过条目，新口径不该顺手把历史形状收紧（那等于替
      用户改判）；
    - 生成过程中报了 `missing_pending` → 丢掉（那时清单还没抽出来，该判定不可能成立）。

    Args:
        item: 模型给出的一条问题。
        allow_missing_pending: 这一时点是否允许判「清单漏登记」。

    Returns:
        归一后的问题；`None` 表示这条不进结论。
    """
    issue = dict(item)
    category = normalize_category(issue.get("category"))
    if category == MISSING_PENDING and not allow_missing_pending:
        return None
    if category is not None:
        issue["severity"] = CATEGORY_SEVERITY[category]
    else:
        # 旧结论形状：只做大小写与空白的规整，不筛条目、不改判
        issue["severity"] = str(issue.get("severity") or "").strip().lower()
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


def render_pending_section(items: list[dict[str, Any]] | None) -> str:
    """重审提示词里那一格：第六类判据 + 当前版本的待确认清单。

    取空列表时**仍然给出第六类**——清单为空恰恰最容易漏登记，不能因为「没有清单」就
    把这类的判定关掉。生成时调用方传的是空串（不调用本函数），因此那一态里既没有这一类，
    也没有清单。

    Args:
        items: 当前版本的待确认清单（后端结构）。

    Returns:
        可直接替换提示词里 `{pending_section}` 的文本。
    """
    rules = (
        "6. **清单漏登记**（medium，`category` 写 `missing_pending`）：正文里有需要用户拍板、"
        "却**没有登记**在下面这份清单里的项。已经登记过的项不算缺陷，不要重复报。"
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


def finalize_verdict(
    verdict: str, *, issues: list[dict[str, Any]], instruction: str, dropped: int
) -> tuple[str, str]:
    """判定词与修改指令的收尾：条目被时点口径全部过滤掉时，不算「有问题」。

    为什么要这一步：生成链路里模型偶尔会自报一条「清单漏登记」，它被服务端丢掉之后，
    若判定仍是 `revise`、指令还留着，改写门槛（`high` 才动笔那条路是看 `issues`；
    没有 `issues` 时按「有指令就改」放行）就会为一条**不存在的缺陷**白改一次。
    指令是照着那些缺陷写的，缺陷没了，指令也该一起清掉。

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


def issues_text(issues: list[dict[str, Any]], *, clip: Any = None) -> str:
    """把结论里的问题排成给改写提示词用的编号列表（带上类别与 `where`）。

    类别用中文标签：改写那一步读的是**这一行**，只给英文标识等于让它自己翻译一遍。

    Args:
        issues: 归一后的问题列表。
        clip: 可选的截断函数（保持调用方原有的 `detail` 长度口径）。

    Returns:
        编号列表；没有问题时的占位句由调用方给。
    """
    lines: list[str] = []
    for index, issue in enumerate(issues, start=1):
        severity = str(issue.get("severity") or "medium")
        category = normalize_category(issue.get("category"))
        label = f"{severity}·{CATEGORY_LABELS[category]}" if category else severity
        detail = str(issue.get("detail") or "")
        if callable(clip):
            detail = str(clip(detail))
        where = str(issue.get("where") or "").strip()
        prefix = f"[{label}]"
        if where:
            prefix += f" {where}"
        lines.append(f"{index}. {prefix}：{detail}")
    return "\n".join(lines)
