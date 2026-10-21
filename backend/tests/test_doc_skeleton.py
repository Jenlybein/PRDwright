"""骨架化与分节切分的行为测试。

这些用例钉住的是「压缩到什么程度才算够用」：骨架不是越短越好——丢了可核对的
事实，审核会反过来误报「覆盖缺失」，然后触发一轮本不必要的改写。
"""

from __future__ import annotations

from backend.services.doc_skeleton import (
    DocSection,
    extract_headings,
    join_sections,
    normalize_heading,
    resolve_where,
    skeletonize,
    split_sections,
    strip_heading,
)

_SAMPLE = (
    "# 演示产品 接口文档\n"
    "\n"
    "## 2. 接口列表\n"
    "\n"
    "| 编号 | 路径 | 说明 |\n"
    "| --- | --- | --- |\n"
    "| F-01 | /api/v1/login | 登录，响应时间 < 200ms |\n"
    "| F-02 | /api/v1/orders | 下单 |\n"
    "\n"
    "## 3. 非功能\n"
    "\n"
    "- 可用性 99.9%\n"
    "- 支持 1000 并发\n"
    "\n"
    "这是一段没有任何数字的说明性文字，它只是把上面的内容换个说法复述了一遍，"
    "对审核没有任何新增信息，应该被丢掉。\n"
    "\n"
    "```json\n"
    "{\n"
    '  "code": 0,\n'
    '  "data": { "very": "long", "and": "verbose" }\n'
    "}\n"
    "```\n"
)


def test_skeleton_keeps_checkable_facts() -> None:
    """编号 / 路径 / 数字这些「可核对的事实」一个都不能丢。"""
    result = skeletonize(_SAMPLE)
    for fact in (
        "# 演示产品 接口文档",
        "## 2. 接口列表",
        "F-01",
        "/api/v1/login",
        "200ms",
        "99.9%",
        "1000 并发",
    ):
        assert fact in result, f"骨架丢了可核对的事实：{fact}"


def test_skeleton_drops_verbose_prose_and_code_bodies() -> None:
    """无数字的复述性散文与代码块体是压缩的主要来源。"""
    result = skeletonize(_SAMPLE)
    assert "说明性文字" not in result
    assert "换个说法复述" not in result
    # 代码块只留围栏首行，块体（含这段 JSON 的特征串）整体丢弃
    assert '"very": "long"' not in result
    assert "```json" in result
    assert len(result) < len(_SAMPLE) * 0.6


def test_skeleton_truncates_and_marks_omission() -> None:
    """截断时必须显式写出省略了多少——否则审核会以为文档就这么短。"""
    long_doc = "\n".join(f"- 第 {index} 条条目" for index in range(3000))
    result = skeletonize(long_doc, max_chars=500)
    assert len(result) < 900
    assert "骨架已截断" in result
    assert "未展示" in result


def test_skeleton_of_empty_text_is_explicit() -> None:
    """空输入返回显式说明而不是空串：提示词里留空会被读成「没有上游材料」。"""
    assert skeletonize("").startswith("（")
    assert skeletonize("   \n  ") != ""


def test_extract_headings_limits_level() -> None:
    # 默认只要 `## `：H1 是文档头，不该成为 `where` 的取值
    assert extract_headings(_SAMPLE) == ["## 2. 接口列表", "## 3. 非功能"]
    assert extract_headings(_SAMPLE, level=1) == ["# 演示产品 接口文档"]


def test_split_keeps_preamble_and_join_roundtrips() -> None:
    """preamble（H1 与前言）必须保留——丢掉会让拼回的全文缺头。"""
    preamble, sections = split_sections(_SAMPLE)
    assert preamble.strip() == "# 演示产品 接口文档"
    assert [section.heading for section in sections] == ["## 2. 接口列表", "## 3. 非功能"]
    assert "F-01" in sections[0].body

    rejoined = join_sections(preamble, sections)
    assert rejoined.startswith("# 演示产品 接口文档")
    assert rejoined.count("## ") == 2
    assert "99.9%" in rejoined


def test_split_without_headings_returns_whole_text_as_preamble() -> None:
    preamble, sections = split_sections("没有标题的一段话")
    assert preamble == "没有标题的一段话"
    assert sections == []


def test_resolve_where_matches_leniently_but_never_invents() -> None:
    """模型常少带 `##` 或只写章节名的一半，要能匹配；但绝不能猜一个近似的。"""
    _, sections = split_sections("## 2. 接口列表\n\n x\n\n## 3. 错误码\n\n y\n")

    # 精确、带序号、只写章节名——三种写法都要落到同一节
    for where in ("## 2. 接口列表", "2. 接口列表", "接口列表"):
        hit = resolve_where(where, sections)
        assert hit is not None and hit.heading == "## 2. 接口列表", where

    assert resolve_where("## 9. 不存在的章节", sections) is None
    assert resolve_where("", sections) is None

    hit = resolve_where("## 3. 错误码", sections)
    assert hit is not None and hit.heading == "## 3. 错误码"


def test_normalize_heading_ignores_marks_and_punctuation() -> None:
    assert normalize_heading("## 2. 接口列表") == normalize_heading("接口列表")
    assert normalize_heading("## 2. 接口列表") == normalize_heading("2、接口列表")


def test_strip_heading_removes_the_duplicated_first_line_only() -> None:
    """模型常把标题行再输出一遍；只去掉开头那处，正文中段的同名标题不动。"""
    assert strip_heading("## A\n\n正文\n\n## A\n\n再一个", "## A") == "正文\n\n## A\n\n再一个"
    assert strip_heading("正文开头", "## A") == "正文开头"


def test_join_sections_handles_empty_preamble() -> None:
    merged = join_sections("", [DocSection(heading="## A", body="正文A")])
    assert merged.strip() == "## A\n\n正文A"
