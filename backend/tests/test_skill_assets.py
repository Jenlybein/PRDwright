"""R2-T6 技能资产加固。

覆盖两条债务：
- **D10 资源常驻缓存**：改模板不再需要重启后端；
- **D8 静默降级**：技能包缺失变成「清单可见 + 可选 fail-fast + 回执可认」。

这些用例全部离线：用临时目录当 skills 根目录，通过环境变量指过去，不碰仓库里真实的
`skills/prd-generator`。
"""

from __future__ import annotations

import pytest

from backend.services import prd_skill


@pytest.fixture()
def skill_root(tmp_path, monkeypatch):
    """一个最小可用的技能包目录：三份资源 + `skill.yaml`。

    fixture 结束时会清掉进程内的读盘缓存，避免临时目录里的条目留到后续用例。
    """
    root = tmp_path / "skills" / "prd-generator"
    (root / "references").mkdir(parents=True)
    for relative_path in prd_skill.PRD_SKILL_ARTIFACTS.values():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"初始内容：{relative_path}\n", encoding="utf-8")
    (root / "skill.yaml").write_text(
        "name: prd-generator\nversion: 0.1.0\ndescription: 测试用技能包\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PRDWRIGHT_SKILLS_DIR", str(tmp_path / "skills"))
    prd_skill.clear_artifact_cache()
    yield root
    prd_skill.clear_artifact_cache()


def _write_longer(path, suffix: str) -> None:
    """改写文件并保证「修改时间 + 大小」两项都变化。

    单纯往同一长度改写内容时，字节数不变、某些文件系统上的时间戳精度也不足，
    那样会让这条用例验不到真正的重载路径。
    """
    path.write_text(f"{path.read_text(encoding='utf-8')}新增行：{suffix}\n" * 2, encoding="utf-8")


# ----------------------------------------------------------------------------
# D10：改文件立刻生效
# ----------------------------------------------------------------------------


def test_artifact_is_reloaded_after_edit(skill_root) -> None:
    """改了模板之后下一次读取就是新内容——不必重启后端。"""
    relative_path = prd_skill.PRD_SKILL_ARTIFACTS["prd_template"]
    first = prd_skill.load_skill_artifact(prd_skill.PRD_SKILL_NAME, relative_path)
    assert first.startswith("初始内容")

    _write_longer(skill_root / relative_path, "模板改了一版")

    second = prd_skill.load_skill_artifact(prd_skill.PRD_SKILL_NAME, relative_path)
    assert second != first
    assert "模板改了一版" in second


def test_unchanged_artifact_is_read_from_cache(skill_root) -> None:
    """内容没变时仍然走缓存：一次生成读一次盘的开销，不该每次调用都付一遍。"""
    relative_path = prd_skill.PRD_SKILL_ARTIFACTS["writing_rules"]
    first = prd_skill.load_skill_artifact(prd_skill.PRD_SKILL_NAME, relative_path)
    assert prd_skill.load_skill_artifact(prd_skill.PRD_SKILL_NAME, relative_path) == first


def test_deleted_artifact_does_not_serve_stale_content(skill_root) -> None:
    """资源被删掉后必须报错，而不是继续供给缓存里的旧内容。"""
    relative_path = prd_skill.PRD_SKILL_ARTIFACTS["field_schema"]
    assert prd_skill.load_skill_artifact(prd_skill.PRD_SKILL_NAME, relative_path)

    (skill_root / relative_path).unlink()

    with pytest.raises(FileNotFoundError):
        prd_skill.load_skill_artifact(prd_skill.PRD_SKILL_NAME, relative_path)


# ----------------------------------------------------------------------------
# D8：缺失可见 + fail-fast
# ----------------------------------------------------------------------------


def test_missing_assets_are_reported_by_name(skill_root) -> None:
    """缺失清单按相对路径给出，让「到底少哪个文件」可以照着排。"""
    assert prd_skill.missing_skill_assets() == []

    (skill_root / prd_skill.PRD_SKILL_ARTIFACTS["prd_template"]).unlink()

    assert prd_skill.missing_skill_assets() == [prd_skill.PRD_SKILL_ARTIFACTS["prd_template"]]


def test_load_artifacts_returns_none_when_any_missing(skill_root) -> None:
    """任一资源缺失就整体降级返回 `None`，由调用方走内置提示词。"""
    (skill_root / prd_skill.PRD_SKILL_ARTIFACTS["writing_rules"]).unlink()

    assert prd_skill.load_prd_skill_artifacts() is None


def test_ensure_skill_assets_only_reports_when_not_strict(skill_root) -> None:
    """默认不拦启动：本地调试缺 skills 也能起服务，只是留下清单给调用方告警。"""
    assert prd_skill.ensure_skill_assets() == []

    (skill_root / prd_skill.PRD_SKILL_ARTIFACTS["field_schema"]).unlink()

    assert prd_skill.ensure_skill_assets() == [prd_skill.PRD_SKILL_ARTIFACTS["field_schema"]]


def test_ensure_skill_assets_fails_fast_when_strict(skill_root) -> None:
    """生产态（strict）缺失即中止启动：漏打包 skills 要在起服务时炸出来。"""
    (skill_root / prd_skill.PRD_SKILL_ARTIFACTS["prd_template"]).unlink()

    with pytest.raises(RuntimeError) as excinfo:
        prd_skill.ensure_skill_assets(strict=True)

    message = str(excinfo.value)
    assert prd_skill.PRD_SKILL_ARTIFACTS["prd_template"] in message
    # 报错里必须带上「已经被解析成哪个目录」和「怎么改」，否则报错等于没报
    assert str(skill_root) in message
    assert "PRDWRIGHT_SKILLS_DIR" in message


# ----------------------------------------------------------------------------
# 版本与指纹
# ----------------------------------------------------------------------------


def test_skill_version_is_read_from_skill_yaml(skill_root) -> None:
    assert prd_skill.skill_version() == "0.1.0"


def test_skill_version_is_unknown_without_yaml(skill_root) -> None:
    (skill_root / "skill.yaml").unlink()
    assert prd_skill.skill_version() == "unknown"


def test_describe_skill_reports_full_identity(skill_root) -> None:
    described = prd_skill.describe_skill()
    assert described is not None
    assert described["name"] == prd_skill.PRD_SKILL_NAME
    assert described["version"] == "0.1.0"
    assert len(described["fingerprint"]) == 12


def test_describe_skill_is_none_when_missing(skill_root) -> None:
    """资源不全就不产出身份条目：「这次没用上技能包」必须是显式的。"""
    (skill_root / prd_skill.PRD_SKILL_ARTIFACTS["writing_rules"]).unlink()
    assert prd_skill.describe_skill() is None


def test_fingerprint_changes_with_content(skill_root) -> None:
    """指纹能感知内容变化：两份回执对不对得上同一套模板，看指纹就够，不必人工改版本号。"""
    before = prd_skill.skill_fingerprint()
    assert len(before) == 12

    _write_longer(skill_root / prd_skill.PRD_SKILL_ARTIFACTS["writing_rules"], "规范调整")

    assert prd_skill.skill_fingerprint() != before


def test_fingerprint_changes_with_version(skill_root) -> None:
    """版本号单独变化也会改指纹——防止「只改了版本号却没改内容」被算成同一套。"""
    before = prd_skill.skill_fingerprint()

    (skill_root / "skill.yaml").write_text(
        "name: prd-generator\nversion: 0.2.0\ndescription: 测试用技能包（升版本）\n",
        encoding="utf-8",
    )

    assert prd_skill.skill_fingerprint() != before
