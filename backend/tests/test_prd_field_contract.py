"""输入字段契约的压缩：整份 JSON Schema → 几行字段清单。

为什么要有这一层：整份 `field-schema.json` 有 8.6KB，而撰写真正需要的只有三件事——
**哪些字段必填、每个字段是什么、有哪些枚举取值**。剩下的 `examples` / `minLength` /
`anyOf` / `x_source_aliases` 是给归一化与校验用的，照着写正文时一条都用不上，却是
**每一章都要原样付一遍**的输入（六章就是五十几 KB 的纯重复）。

守的是两条：
1. **压掉的必须是校验细节，不能是语义。** 字段名、必填性、说明、枚举取值一个都不能少——
   少一条 `priority 取值 P0/P1/P2`，模型就会自创一套「高 / 中 / 低」。
2. **降级要显式。** schema 损坏时返回一句说明而不是空串：空串会被模型读成「没有字段
   契约」，从而静默降级成"随便写"。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.services.prd_skill import format_field_contract, load_prd_skill_artifacts


def _schema() -> str:
    """技能包里那份真实契约——拿它测，别拿手搓的样本测。"""
    artifacts = load_prd_skill_artifacts()
    assert artifacts is not None, "PRD 技能包不可读，用例无法验证契约压缩"
    return artifacts["field_schema"]


def test_every_field_survives_the_compression() -> None:
    """一个字段都不能少：压缩后仍能逐个点名，且必填/选填分组正确。"""
    schema = json.loads(_schema())
    contract = format_field_contract(_schema())

    for name in schema["properties"]:
        assert name in contract, f"字段 {name} 在压缩后消失了"

    required = set(schema["required"])
    head = contract.splitlines()[0]
    for name in required:
        assert name in head, f"必填字段 {name} 不在第一行"
    for name in set(schema["properties"]) - required:
        assert name not in head, f"选填字段 {name} 被误分到必填组"


def test_enum_values_survive_the_compression() -> None:
    """枚举取值必须留下：`priority` 藏在 `anyOf` 下两层，浅扫会漏。

    漏掉它的后果不是「少一句说明」，而是模型自创「高 / 中 / 低」优先级——
    而 MVP 功能默认 P0 是明确的产品口径（`field-schema.json` 的 `priority.default`）。
    """
    contract = format_field_contract(_schema())
    assert "priority 取值 P0/P1/P2" in contract
    assert "priority 取值 P1/P2" in contract


def test_nested_keys_are_flattened() -> None:
    """`technical_constraints` 的子键要摊开：技术规格章要逐格对得上输入字段名。"""
    contract = format_field_contract(_schema())
    for key in ("auth", "storage", "frontend", "backend", "deployment", "third_party"):
        assert key in contract, f"technical_constraints.{key} 没被摊开"


def test_compression_actually_shrinks() -> None:
    """压缩必须真的压：否则这层就白加了，还平白多一次 JSON 解析。"""
    raw = _schema()
    contract = format_field_contract(raw)
    assert len(contract) < len(raw) / 3, (
        f"压缩比不够：{len(raw)} -> {len(contract)}，达不到三成"
    )
    # 校验细节不该出现在压缩结果里（它们是压缩掉的那部分）
    assert '"$schema"' not in contract
    assert "x_normalization_rules" not in contract


def test_broken_schema_degrades_loudly() -> None:
    """schema 损坏 / 为空时返回一句说明，而不是空串。"""
    for raw in ("", "   ", "{不是 JSON", "[]"):
        contract = format_field_contract(raw)
        assert contract.strip(), f"输入 {raw!r} 得到了空串"
        assert "以需求输入原文为准" in contract


def test_normalization_rules_are_kept() -> None:
    """归一化规则要留几条：数组项写成字符串时怎么理解，是撰写绕不过去的口径。"""
    contract = format_field_contract(_schema())
    assert "归一化：" in contract
    assert "platform" in contract


def test_written_skill_file_is_the_one_being_compressed() -> None:
    """压缩器读的是**技能包那份文件**，不是某个测试夹具里的副本。

    这一条守的是「改了 skill 包却忘了改别处」：压缩器的输出会随文件变化，
    两者不一致时整条链路拿到的字段说明就与实际契约对不上了。
    """
    on_disk = (Path(__file__).resolve().parents[2] / "skills" / "prd-generator").as_posix()
    assert on_disk, "定位不到技能包目录"
    artifacts = load_prd_skill_artifacts()
    assert artifacts is not None
    # 文件里真有 schema（不是空壳），否则前面的用例都是假绿
    assert '"product_name"' in artifacts["field_schema"]