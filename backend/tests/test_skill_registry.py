"""技能注册表：扫描 `skills/` 建索引、按产物找包、资源加载与自检。

这些用例全部离线：用临时目录当 skills 根目录（`temp_skills_root` 夹具），
通过环境变量指过去，不碰仓库里真实的 `skills/prd-generator`。
（唯一一条例外是最后那条「与仓库真实技能包对齐」，它刻意要验真实文件。）

一条贯穿全篇的前提：**技能包自己声明它服务于什么、需要哪些资源**，
所以「加一个技能包」应当等于「加一个目录」——下面几条用例就是在把这句话落成断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.skill_registry import (
    audit_skills,
    clear_caches,
    describe_kind,
    describe_manifest,
    ensure_skills,
    load_artifact,
    load_skill_artifacts,
    manifest_for_kind,
    missing_artifacts,
    scan_skills,
    skill_fingerprint,
)

from conftest import PRD_SKILL_ARTIFACTS, write_longer, write_skill_package


# ----------------------------------------------------------------------------
# 扫描：目录即注册
# ----------------------------------------------------------------------------


def test_scan_reads_declared_identity(temp_skills_root) -> None:
    """manifest 的每个字段都来自技能包自己的声明。"""
    write_skill_package(temp_skills_root, "prd-generator", version="0.2.0")

    manifests, problems = scan_skills()

    assert problems == []
    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest.name == "prd-generator"
    assert manifest.version == "0.2.0"
    assert manifest.applies_to == ("prd",)
    assert manifest.artifacts == PRD_SKILL_ARTIFACTS
    # 资源读取以解析时定位到的目录为准，不再按名字二次查找
    assert manifest.root == temp_skills_root / "prd-generator"


def test_manifest_for_kind_matches_declaration(temp_skills_root) -> None:
    write_skill_package(temp_skills_root, "prd-generator", applies_to=("prd",))

    assert manifest_for_kind("prd") is not None
    assert manifest_for_kind("prompts") is None


def test_new_skill_becomes_visible_without_code_change(temp_skills_root) -> None:
    """**注册表存在的理由**：产物归属由技能包声明，改了声明立刻改归属，不必动 Python。

    这就是「加一个技能包 = 加一个目录」的可执行证明。
    """
    skill_dir = write_skill_package(temp_skills_root, "prd-generator", applies_to=("prd",))
    assert manifest_for_kind("prompts") is None

    # 只改技能包自己声明的归属，代码一行不动
    (skill_dir / "skill.yaml").write_text(
        "name: prd-generator\nversion: 0.1.0\napplies_to:\n  - prompts\nreferences: []\n",
        encoding="utf-8",
    )

    assert manifest_for_kind("prd") is None
    assert manifest_for_kind("prompts") is not None


def test_skill_without_applies_to_is_listed_but_never_matched(temp_skills_root) -> None:
    """不声明适用产物的包仍会被列出（体检要展示它），但不会被任何产物选中，也不算错误。"""
    write_skill_package(temp_skills_root, "manual-only", applies_to=())

    manifests, problems = scan_skills()

    assert [manifest.name for manifest in manifests] == ["manual-only"]
    assert manifests[0].applies_to == ()
    assert problems == []
    assert manifest_for_kind("prd") is None


def test_broken_manifest_is_reported_not_silently_skipped(temp_skills_root) -> None:
    """`skill.yaml` 写坏了要报出来——「我加了技能包怎么没生效」必须有答案。"""
    skill_dir = temp_skills_root / "broken-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text("name: [未闭合\n", encoding="utf-8")

    manifests, problems = scan_skills()

    assert manifests == []
    assert len(problems) == 1
    assert problems[0].skill == "broken-skill"
    assert "元数据无法解析" in problems[0].reason


def test_directory_without_manifest_is_not_a_skill(temp_skills_root) -> None:
    """目录里没有 `skill.yaml` 就不是技能包：不列入，也不算问题。"""
    (temp_skills_root / "just-a-folder").mkdir()

    manifests, problems = scan_skills()

    assert manifests == []
    assert problems == []


def test_two_skills_claiming_same_kind_are_reported(temp_skills_root) -> None:
    """同一产物被两个包声明：行为确定（按名字取第一个），但必须报出来而不是悄悄生效。"""
    write_skill_package(temp_skills_root, "alpha-skill", applies_to=("prd",))
    write_skill_package(temp_skills_root, "beta-skill", applies_to=("prd",))

    manifests, problems = audit_skills()

    assert len(manifests) == 2
    assert any("同时声明服务于 prd" in item.reason for item in problems)
    assert manifest_for_kind("prd").name == "alpha-skill"


def test_missing_key_falls_back_to_filename(temp_skills_root) -> None:
    """`references` 项不写 `key` 时用文件名推（连字符转下划线）——确定性规则，不是猜。"""
    skill_dir = temp_skills_root / "no-key-skill"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "references" / "writing-rules.md").write_text("内容\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        "name: no-key-skill\nversion: 0.1.0\napplies_to:\n  - prd\n"
        "references:\n  - path: references/writing-rules.md\n",
        encoding="utf-8",
    )

    manifest = manifest_for_kind("prd")

    assert manifest.artifacts == {"writing_rules": "references/writing-rules.md"}


# ----------------------------------------------------------------------------
# 资源读取：改了立刻生效，没了立刻改口
# ----------------------------------------------------------------------------


def test_artifact_is_reloaded_after_edit(temp_skills_root) -> None:
    """改了资源之后下一次读取就是新内容——不必重启后端（ADR-017）。"""
    write_skill_package(temp_skills_root, "prd-generator")
    relative_path = PRD_SKILL_ARTIFACTS["prd_template"]
    first = load_artifact("prd-generator", relative_path)
    assert first.startswith("初始内容")

    write_longer(temp_skills_root / "prd-generator" / relative_path, "模板改了一版")

    second = load_artifact("prd-generator", relative_path)
    assert second != first
    assert "模板改了一版" in second


def test_unchanged_artifact_is_not_reread(temp_skills_root, monkeypatch) -> None:
    """文件戳没变时不重新读盘：一次生成会读十几次，这个开销不该每次调用都付。"""
    write_skill_package(temp_skills_root, "prd-generator")
    relative_path = PRD_SKILL_ARTIFACTS["writing_rules"]
    first = load_artifact("prd-generator", relative_path)

    reads: list[Path] = []
    original_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        reads.append(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    assert load_artifact("prd-generator", relative_path) == first
    assert reads == []


def test_deleted_artifact_does_not_serve_stale_content(temp_skills_root) -> None:
    """资源被删掉后必须报错，而不是继续供给缓存里的旧内容。"""
    write_skill_package(temp_skills_root, "prd-generator")
    relative_path = PRD_SKILL_ARTIFACTS["field_schema"]
    assert load_artifact("prd-generator", relative_path)

    (temp_skills_root / "prd-generator" / relative_path).unlink()

    with pytest.raises(FileNotFoundError):
        load_artifact("prd-generator", relative_path)


def test_missing_artifact_is_not_masked_by_cache(temp_skills_root) -> None:
    """**删掉资源后体检必须立刻改口**：资源存在性不能进缓存。

    这是本模块最该守住的一条——「文件已经没了却还在报一切正常」正是我们花整轮
    要消灭的那种静默。
    """
    write_skill_package(temp_skills_root, "prd-generator")
    _, first_problems = audit_skills()
    assert first_problems == []

    (temp_skills_root / "prd-generator" / PRD_SKILL_ARTIFACTS["field_schema"]).unlink()

    manifests, second_problems = audit_skills()

    assert len(second_problems) == 1
    assert "缺少技能资源" in second_problems[0].reason
    # 出问题的包仍然要被列出来，否则体检展示不出「是谁缺了」
    assert [manifest.name for manifest in manifests] == ["prd-generator"]


def test_missing_artifacts_lists_relative_paths(temp_skills_root) -> None:
    """缺失清单按相对路径给出，让「到底少哪个文件」可以照着排。"""
    write_skill_package(temp_skills_root, "prd-generator")
    manifest = manifest_for_kind("prd")
    assert missing_artifacts(manifest) == []

    (temp_skills_root / "prd-generator" / PRD_SKILL_ARTIFACTS["prd_template"]).unlink()

    assert missing_artifacts(manifest) == [PRD_SKILL_ARTIFACTS["prd_template"]]


# ----------------------------------------------------------------------------
# 身份与指纹
# ----------------------------------------------------------------------------


def test_fingerprint_changes_with_content(temp_skills_root) -> None:
    """指纹能感知内容变化：两份回执对不对得上同一套资产，看指纹就够。"""
    write_skill_package(temp_skills_root, "prd-generator")
    manifest = manifest_for_kind("prd")
    before = skill_fingerprint(manifest)
    assert len(before) == 12

    write_longer(
        temp_skills_root / "prd-generator" / PRD_SKILL_ARTIFACTS["writing_rules"], "规范调整"
    )

    assert skill_fingerprint(manifest) != before


def test_fingerprint_changes_with_version(temp_skills_root) -> None:
    """版本号单独变化也会改指纹——防止「只改了版本号没改内容」被算成同一套。"""
    write_skill_package(temp_skills_root, "prd-generator")
    before = skill_fingerprint(manifest_for_kind("prd"))

    write_skill_package(temp_skills_root, "prd-generator", version="0.2.0")

    assert skill_fingerprint(manifest_for_kind("prd")) != before


def test_fingerprint_is_stable_for_same_content(temp_skills_root) -> None:
    """同一份内容，走缓存与重新读盘得到的指纹一致——缓存不能悄悄改变口径。"""
    write_skill_package(temp_skills_root, "prd-generator")
    first = skill_fingerprint(manifest_for_kind("prd"))

    clear_caches()

    assert skill_fingerprint(manifest_for_kind("prd")) == first


def test_describe_manifest_is_none_when_incomplete(temp_skills_root) -> None:
    """资源不全就不产出身份条目：「这次没用上技能包」必须是显式的（债务 D8）。"""
    write_skill_package(temp_skills_root, "prd-generator")
    manifest = manifest_for_kind("prd")
    assert describe_manifest(manifest)["name"] == "prd-generator"

    (temp_skills_root / "prd-generator" / PRD_SKILL_ARTIFACTS["writing_rules"]).unlink()

    assert describe_manifest(manifest) is None


def test_describe_kind_is_none_without_skill(temp_skills_root) -> None:
    """没有技能包声明服务于该产物时返回 `None`——它就是「降级」的信号。"""
    write_skill_package(temp_skills_root, "prd-generator", applies_to=("prd",))

    assert describe_kind("prompts") is None
    assert describe_kind("prd")["name"] == "prd-generator"


# ----------------------------------------------------------------------------
# 启动自检
# ----------------------------------------------------------------------------


def test_ensure_skills_reports_problems_without_strict(temp_skills_root) -> None:
    """默认不拦启动：本地调试缺 skills 也能起服务，只是把问题清单交给调用方告警。"""
    write_skill_package(temp_skills_root, "prd-generator")
    manifests, problems = ensure_skills()
    assert [manifest.name for manifest in manifests] == ["prd-generator"]
    assert problems == []

    (temp_skills_root / "prd-generator" / PRD_SKILL_ARTIFACTS["field_schema"]).unlink()

    _, problems = ensure_skills()
    assert len(problems) == 1
    assert "field-schema.json" in problems[0].reason


def test_ensure_skills_fails_fast_when_strict(temp_skills_root) -> None:
    """生产态（strict）有问题即中止启动：漏打包 skills 要在起服务时炸出来。"""
    write_skill_package(temp_skills_root, "prd-generator")
    (temp_skills_root / "prd-generator" / PRD_SKILL_ARTIFACTS["prd_template"]).unlink()

    with pytest.raises(RuntimeError) as excinfo:
        ensure_skills(strict=True)

    message = str(excinfo.value)
    assert PRD_SKILL_ARTIFACTS["prd_template"] in message
    # 报错里必须带上「已经解析成哪个目录」和「怎么改」，否则报错等于没报
    assert str(temp_skills_root) in message
    assert "PRDWRIGHT_SKILLS_DIR" in message


def test_ensure_skills_returns_manifests_for_startup_log(temp_skills_root) -> None:
    """自检顺带把「这次加载了哪些包、各自服务什么」交出来，启动日志不必再扫一遍。"""
    write_skill_package(temp_skills_root, "prd-generator", version="0.3.0")

    manifests, problems = ensure_skills()

    assert problems == []
    assert [(m.name, m.version, m.applies_to) for m in manifests] == [
        ("prd-generator", "0.3.0", ("prd",))
    ]


# ----------------------------------------------------------------------------
# 与仓库真实技能包对齐（防格式漂移）
# ----------------------------------------------------------------------------


def test_repo_prd_skill_is_registered(monkeypatch) -> None:
    """仓库里真实的 `skills/prd-generator` 必须能被扫到，且声明服务于 prd。

    这条补的是「格式漂移」：`skill.yaml` 的写法一旦与解析器对不上，上面那些临时目录
    里的用例全过也说明不了问题。所以它刻意不指环境变量、走真实的目录发现。
    """
    monkeypatch.delenv("PRDWRIGHT_SKILLS_DIR", raising=False)
    clear_caches()

    manifest = manifest_for_kind("prd")

    assert manifest is not None
    assert manifest.name == "prd-generator"
    assert "prd" in manifest.applies_to
    assert set(manifest.artifacts) == set(PRD_SKILL_ARTIFACTS)


# ----------------------------------------------------------------------------
# 全量资源加载：生成管线「整段注入」的取数入口
# ----------------------------------------------------------------------------


def test_load_skill_artifacts_returns_all_declared_resources(temp_skills_root) -> None:
    """生成管线不再逐字段拿资源，而是一次取回声明里的全部资源（语义键 -> 全文）。"""
    write_skill_package(
        temp_skills_root,
        "api-docs-generator",
        applies_to=("api_docs",),
        artifacts={"api_specs": "references/api-specs.md"},
    )

    artifacts = load_skill_artifacts("api_docs")

    assert artifacts is not None
    assert set(artifacts) == {"api_specs"}
    assert "初始内容：references/api-specs.md" in artifacts["api_specs"]


def test_load_skill_artifacts_returns_none_without_matching_skill(temp_skills_root) -> None:
    """没有技能包声明服务于该产物时返回 None——这就是「降级到内置提示词」的信号。"""
    write_skill_package(temp_skills_root, "prd-generator", applies_to=("prd",))

    assert load_skill_artifacts("api_docs") is None
    assert load_skill_artifacts("prompts") is None


def test_load_skill_artifacts_returns_none_when_artifact_missing(temp_skills_root) -> None:
    """声明了资源却读不到文件时不抛异常、返回 None，让调用方走降级而不是半截注入。"""
    write_skill_package(
        temp_skills_root,
        "api-docs-generator",
        applies_to=("api_docs",),
        artifacts={"api_specs": "references/api-specs.md"},
    )
    (temp_skills_root / "api-docs-generator" / "references" / "api-specs.md").unlink()

    assert load_skill_artifacts("api_docs") is None


# ----------------------------------------------------------------------------
# R2-T9 新技能包：格式漂移锁（与仓库真实包对齐）
# ----------------------------------------------------------------------------


def test_repo_api_docs_and_prompts_skills_are_registered_and_loadable(monkeypatch) -> None:
    """R2-T9 迁出的两个技能包必须能被扫到、且资源可全量加载——锁住「接口规范 / 提示词规范
    改由技能包驱动」这条落地路径，任何把规范写回散落位置的改动都会在这里炸出来。

    刻意不指环境变量、走真实目录发现，与 `test_repo_prd_skill_is_registered` 同一口径。
    """
    monkeypatch.delenv("PRDWRIGHT_SKILLS_DIR", raising=False)
    clear_caches()

    api_manifest = manifest_for_kind("api_docs")
    prompts_manifest = manifest_for_kind("prompts")

    assert api_manifest is not None
    assert api_manifest.name == "api-docs-generator"
    assert prompts_manifest is not None
    assert prompts_manifest.name == "prompts-generator"

    api_artifacts = load_skill_artifacts("api_docs")
    prompts_artifacts = load_skill_artifacts("prompts")

    assert api_artifacts is not None
    assert set(api_artifacts) == set(api_manifest.artifacts)
    assert all(content.strip() for content in api_artifacts.values())

    assert prompts_artifacts is not None
    assert set(prompts_artifacts) == set(prompts_manifest.artifacts)
    assert all(content.strip() for content in prompts_artifacts.values())

