"""待确认清单的**兜底**形态：模型写成「附：待确认问题」人读列表时怎么办。

2026-11-18 线上反馈：PRD 正文尾部多出一段「附：待确认问题」编号列表——界面上既没有
「待确认问题」容器，正文里又挂着这段本该是元数据的附录。

规范形态是 HTML 注释标记里的 JSON（见 `render_spec()`），但模型没照办时，正文**不能**
就这么放行。这里的用例钉住兜底口径：识别附录 → 从正文剥离 → 解析成清单项。
"""

from __future__ import annotations

from backend.services.pending_items import extract, render_spec

_BODY = """# 演示产品 产品需求文档（PRD）

## 1. 项目背景

这个产品要把模糊想法变成清晰需求。

## 2. 功能需求

### 2.1 需求澄清对话

AI 与用户对话，逐步问清需求。
"""

# 线上真实形态（节选自用户报的那份 PRD 的附录）
_APPENDIX = """
## 附：待确认问题

1. 产品对外主名称统一为 PRDWright 还是 Vibe Coding 需求工作台 —— 影响章节：1.1、全文；需要产品负责人拍板
2. 账号体系的具体形态（注册方式、找回密码、会话有效期） —— 影响章节：2.1（F-01）、4；需要产品负责人拍板
3. 首页任务列表展示字段、排序规则与新建入口交互 —— 影响章节：2.1（F-02）、3；需要产品负责人拍板
"""


def test_legacy_appendix_is_stripped_and_parsed() -> None:
    """人读附录要被剥离并转成清单项，而不是留在正文里。"""
    clean, items = extract(_BODY + _APPENDIX)

    assert len(items) == 3, "三条附录项都应转成待确认项"
    # 旧格式没有 P 等级：统一按中性档 P3 参与澄清，既不假装分级也不整批丢弃
    assert all(item["importance"] == "P3" for item in items)
    assert items[0]["question"].startswith("产品对外主名称统一为")
    assert items[0]["sections"] == ["1.1", "全文"]
    assert items[0]["decider"] == "产品负责人"
    assert items[2]["sections"] == ["2.1（F-02）", "3"]

    # 正文里不能再看到这段附录
    assert "附：待确认问题" not in clean
    assert clean.rstrip().endswith("AI 与用户对话，逐步问清需求。")
    assert "PRDWright 还是 Vibe Coding" not in clean


def test_api_docs_heading_variant_is_also_recognised() -> None:
    """接口文档写的是「### 待确认项」而不是「附：待确认问题」（线上那次就是它漏掉的）。

    2026-11-18 第二次线上反馈：接口文档生成完成后「待确认问题」容器是空的。
    查库：`document_versions.pending_items = "[]"`，正文尾部却挂着 `### 待确认项` 的
    编号列表。判据只认「问题 / 事项 / 清单」三个后缀，**「项」不在其中**，于是整段被
    当成正文放行。三种后缀必须能组合（`待确认问题清单`）也要能单用（`待确认项`）。
    """
    clean, items = extract(
        _BODY
        + """
### 待确认项

1. 登录态 7 天与 refresh_token 30 天的关系，需产品拍板。
2. 注册后是否免登录，需产品在 PRD F-01 补充验收后再调整。
"""
    )

    assert len(items) == 2, "「待确认项」标题下的两条都要转成待确认项"
    assert items[0]["question"].startswith("登录态 7 天")
    # 附录不能留在正文里：界面上有容器、正文里就不该再挂一份
    assert "待确认项" not in clean
    assert clean.rstrip().endswith("AI 与用户对话，逐步问清需求。")


def test_heading_variants_do_not_swallow_prose() -> None:
    """放宽后缀不等于乱抓：正常章节标题里带「待确认」半句不算附录。"""
    clean, items = extract(
        _BODY
        + """
## 待确认的处理方式

这一节讲的是我们怎么处理待确认项，属于正文。
"""
    )

    assert items == []
    assert "待确认的处理方式" in clean


def test_legacy_appendix_folded_lines_join_one_item() -> None:
    """长条目被折行时不能拆成两条（线上那份附录就有折行）。"""
    doc = _BODY + """
## 附：待确认问题

1. 表单必填校验规则与字段格式约束
   影响章节：2.1（F-03）；需要产品负责人拍板
2. AI 判定「信息足够」的规则与手动结束的触发条件 —— 影响章节：2.1（F-04）
"""
    clean, items = extract(doc)

    assert len(items) == 2
    assert "影响章节" not in items[0]["question"]
    assert items[0]["sections"] == ["2.1（F-03）"]
    assert "AI 判定" in items[1]["question"]


def test_mid_document_pending_heading_is_not_an_appendix() -> None:
    """正文中间出现「待确认」章节（不是附录）时不许剥——否则会吃掉真正文。"""
    doc = """# 演示产品

## 待确认问题

这一节是正文的一部分，讲的是我们怎么管理待确认项。

## 1. 项目背景

正文很长很长，把这份文档撑到两段以上，确保那个标题不在后半段。
"""
    clean, items = extract(doc)

    assert items == []
    assert "## 待确认问题" in clean
    assert "这一节是正文的一部分" in clean


def test_appendix_heading_without_items_is_left_alone() -> None:
    """只有标题没有条目：不动正文（避免把一段空附录当清单解析出 0 条还剥掉标题）。"""
    doc = _BODY + "\n## 附：待确认问题\n\n（暂无）\n"
    clean, items = extract(doc)

    assert items == []
    assert "附：待确认问题" in clean


def test_render_spec_forbids_the_human_appendix_shape() -> None:
    """提示词要明写「不要写成人读附录」——兜底只是兜底，不该成为常规路径。"""
    spec = render_spec()

    assert "不要" in spec and "附：待确认问题" in spec
    assert "只能" in spec, "要说明清单只能放在标记块里"
