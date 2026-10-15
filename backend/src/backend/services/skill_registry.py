"""技能包注册表：把「哪个产物用哪个技能包」从代码搬进技能包自己的声明里。

## 它解决的问题

在此之前，「PRD 生成用 `prd-generator`」「这个包里哪三个文件是必需的」两件事都写死在
Python 里。后果是**加一个新技能包必须改代码**，而改代码要重启后端，重启又会把正在跑的
生成任务判死（`job_service.reap_orphans`）——一个本该是「加个目录」的动作，被拖成了
「改代码 + 重启 + 顺手废掉别人正在跑的生成」。

现在这两件事由技能包在自己的 `skill.yaml` 里声明：

```yaml
applies_to: [prd]                       # 我服务于哪些产物（取值 = 任务类型 kind）
references:
  - key: writing_rules                  # 生成管线按这个语义键取内容
    path: references/writing-rules.md
```

程序扫一遍 `<skills 根目录>/*/skill.yaml` 就建好了索引，于是「加一个技能包」退化成
「加一个目录」。**目录即注册**，没有第二处需要同步修改的地方。

## 缓存

两类缓存，口径一致（都是 R2-T6 / ADR-017 定下的）：

- **资源内容**按 `(纳秒修改时间, 字节数)` 缓存：改了立刻生效、没改就不重复读盘；
- **元数据解析结果**同理按 `skill.yaml` 的文件戳缓存。

两者都**只在读成功之后才写缓存**，所以文件补齐后下一次调用立刻能读到；文件消失时
连同缓存条目一起丢掉，避免「文件已经没了还在拿旧内容生成」。

⚠️ 但**资源缺失的判定不进缓存**：删掉一个资源文件不会改动 `skill.yaml`，若把
「缺不缺文件」也缓存起来，删完文件还会继续报「一切正常」——那正是这套机制最该避免的
静默。所以 `audit_skills()` 每次都真查一遍存在性（就是几次 `is_file`，可以忽略）。

## 元数据读不动怎么办

**不静默**。`scan_skills()` 除可用 manifest 之外还会返回问题清单，供启动自检、
`/health` 与日志使用——「我明明加了技能包怎么没生效」必须能在这些地方看到答案。
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 技能根目录的环境变量名（部署时后端与 skills 目录可能不在同一棵目录树里）
SKILLS_DIR_ENV = "PRDWRIGHT_SKILLS_DIR"

# 技能包的自述文件名
MANIFEST_NAME = "skill.yaml"


class SkillManifestError(Exception):
    """`skill.yaml` 存在但读不出身份（YAML 语法错、顶层结构不是键值对等）。"""


@dataclass(frozen=True)
class SkillManifest:
    """一个技能包的自述身份，由它的 `skill.yaml` 解析而来。

    `root` 是解析时定位到的技能包目录：资源读取一律以它为准，不走「按名字再找一次
    目录」——少一次查找就少一处两处结论对不上的可能。
    """

    name: str
    version: str
    applies_to: tuple[str, ...]
    artifacts: dict[str, str]
    root: Path

    def artifact_path(self, key: str) -> Path:
        """按语义键取资源的绝对路径。"""
        return self.root / self.artifacts[key]


@dataclass(frozen=True)
class SkillProblem:
    """一个技能包暴露出来的问题，供自检 / 体检 / 日志展示。

    `skill` 用目录名（元数据都读不动时就只剩这个身份可用）；
    `reason` 是给人看的原因，不做机器判据——真正的判据是「问题清单非空」。
    """

    skill: str
    reason: str


# ---------------------------------------------------------------------------
# 目录定位
# ---------------------------------------------------------------------------


def skills_root() -> Path:
    """定位 skills 根目录。

    查找顺序：
    1. 环境变量 `PRDWRIGHT_SKILLS_DIR`（部署时显式指定，优先）；
    2. 从当前文件向上逐级查找 `<祖先目录>/skills`；
    3. 兜底按目录层级推算（`services/ → backend/ → src/ → backend/ → 仓库根`）。
    """
    env_dir = os.environ.get(SKILLS_DIR_ENV)
    if env_dir:
        return Path(env_dir)

    here = Path(__file__).resolve()
    for parent in here.parents[:6]:
        candidate = parent / "skills"
        if candidate.is_dir():
            return candidate
    return here.parents[4] / "skills"


def resolve_skill_dir(skill_name: str) -> Path:
    """按技能包名定位其目录（`<skills 根目录>/<名称>`）。"""
    return skills_root() / skill_name


# ---------------------------------------------------------------------------
# 资源读取（带文件戳缓存）
# ---------------------------------------------------------------------------

# `资源绝对路径 -> (文件戳, 内容)`
_ARTIFACT_CACHE: dict[str, tuple[tuple[int, int], str]] = {}


def _file_stamp(path: Path) -> tuple[int, int]:
    """文件戳：纳秒级修改时间 + 字节数。

    双条件而不是只有秒级 mtime：同一秒内连续保存时时间戳可能不变，而字节数多半会变。
    """
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


def read_text_cached(path: Path) -> str:
    """读取技能包内的一个文件，按文件戳决定是否复用缓存。

    Args:
        path: 资源文件的绝对路径。

    Returns:
        文本内容（UTF-8）。

    Raises:
        FileNotFoundError: 文件不存在。此时会同时清掉同名缓存条目，
            避免「文件已经没了还在拿旧内容生成」。
    """
    cache_key = str(path)
    try:
        stamp = _file_stamp(path)
    except FileNotFoundError as exc:
        _ARTIFACT_CACHE.pop(cache_key, None)
        raise FileNotFoundError(
            f"技能资源缺失：{path}"
            f"（可用环境变量 {SKILLS_DIR_ENV} 指定 skills 根目录，"
            "或在部署产物中打包该目录）"
        ) from exc

    cached = _ARTIFACT_CACHE.get(cache_key)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    text = path.read_text(encoding="utf-8")
    _ARTIFACT_CACHE[cache_key] = (stamp, text)
    return text


def load_artifact(skill_name: str, relative_path: str) -> str:
    """按「技能包名 + 包内相对路径」读取资源。

    多数调用方应改用 `SkillManifest.artifact_path` + `read_text_cached`：
    它们拿到的是解析当时就已经定位好的目录，不会二次查找。
    """
    return read_text_cached(resolve_skill_dir(skill_name) / relative_path)


# ---------------------------------------------------------------------------
# 元数据解析
# ---------------------------------------------------------------------------

# `skill.yaml 的绝对路径 -> (文件戳, manifest)`。
# 键用绝对路径而不是技能包目录名：不同 skills 根目录下可能有两个同名技能包
# （用例的临时目录、部署时切换的资产目录），按名字做键会让它们互相顶掉。
_MANIFEST_CACHE: dict[str, tuple[tuple[int, int], SkillManifest]] = {}

# `skills 根目录 -> (各 skill.yaml 文件戳的组合, 扫描结果)`
_SCAN_CACHE: dict[str, tuple[tuple[tuple[str, tuple[int, int] | None], ...], tuple[list[SkillManifest], list[SkillProblem]]]] = {}


def _as_list(value: Any) -> list[Any]:
    """把「可能是标量、可能是列表」的 YAML 值统一成列表。

    技能包作者写 `applies_to: prd` 与 `applies_to: [prd]` 都合法，别让人为了一个
    元素去记括号。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _key_from_path(path: str) -> str:
    """从资源路径推出语义键：`references/writing-rules.md` → `writing_rules`。

    这是 `key` 字段缺省时的回落规则（确定性规则，不是猜）。显式写 `key` 更清楚，
    也不会因为改文件名而悄悄改掉契约。
    """
    return Path(path).stem.replace("-", "_")


def _parse_manifest(raw: str, dir_name: str, root: Path) -> SkillManifest:
    """把 `skill.yaml` 的文本解析成 manifest。

    Raises:
        SkillManifestError: YAML 语法错，或顶层不是键值对。
    """
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SkillManifestError(f"YAML 解析失败：{exc}") from exc

    if not isinstance(data, dict):
        raise SkillManifestError("顶层不是键值对结构")

    artifacts: dict[str, str] = {}
    for item in _as_list(data.get("references")):
        if not isinstance(item, dict):
            continue
        relative_path = str(item.get("path") or "").strip()
        if not relative_path:
            continue
        key = str(item.get("key") or "").strip() or _key_from_path(relative_path)
        artifacts[key] = relative_path

    declared_name = str(data.get("name") or "").strip()
    name = declared_name or dir_name
    if declared_name and declared_name != dir_name:
        # 不影响运行（资源读取以解析时定位的 root 为准），但回执上的名字与目录对不上，
        # 将来排查「这到底是哪个包」时会多绕一圈，所以留痕。
        logger.warning(
            "技能包声明名与目录名不一致：skill.yaml 写的是 %s，目录是 %s",
            declared_name,
            dir_name,
            extra={"event": "skill.name_mismatch", "declared": declared_name, "directory": dir_name},
        )

    return SkillManifest(
        name=name,
        version=str(data.get("version") or "").strip() or "unknown",
        applies_to=tuple(
            str(item).strip() for item in _as_list(data.get("applies_to")) if str(item).strip()
        ),
        artifacts=artifacts,
        root=root,
    )


def load_manifest(skill_name: str) -> SkillManifest | None:
    """读取单个技能包的 manifest；`skill.yaml` 不在位时返回 `None`。

    Raises:
        SkillManifestError: `skill.yaml` 存在但解析不了（调用方应把它当成一个问题报出来，
            而不是当成「这个技能包不存在」）。
    """
    root = resolve_skill_dir(skill_name)
    manifest_path = root / MANIFEST_NAME
    cache_key = str(manifest_path)
    try:
        stamp = _file_stamp(manifest_path)
    except FileNotFoundError:
        _MANIFEST_CACHE.pop(cache_key, None)
        return None

    cached = _MANIFEST_CACHE.get(cache_key)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    manifest = _parse_manifest(read_text_cached(manifest_path), skill_name, root)
    _MANIFEST_CACHE[cache_key] = (stamp, manifest)
    return manifest


def _skill_dirs(root: Path) -> list[Path]:
    """列出根目录下所有带 `skill.yaml` 的子目录（按名排序，保证结果稳定）。"""
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return []
    return sorted(
        (path for path in entries if path.is_dir() and (path / MANIFEST_NAME).is_file()),
        key=lambda path: path.name,
    )


def _safe_stamp(path: Path) -> tuple[int, int] | None:
    """文件戳；读不到（不存在 / 权限）返回 `None`——只用于算缓存键，不该抛异常。"""
    try:
        return _file_stamp(path)
    except OSError:
        return None


def scan_skills() -> tuple[list[SkillManifest], list[SkillProblem]]:
    """扫描 skills 根目录，返回「可用的技能包」与「元数据层面的问题」。

    这里**只解析元数据、不检查资源是否在位**：删掉资源文件不会改动 `skill.yaml`，
    若把存在性判定也塞进这份缓存里，删完文件还会继续报「一切正常」。
    资源检查请用 `missing_artifacts()` / `audit_skills()`。

    Returns:
        `(manifests, problems)`。`problems` 目前只包含「`skill.yaml` 读不动」这一类。
    """
    root = skills_root()
    dirs = _skill_dirs(root)
    stamps = tuple(
        (
            str(path),
            _safe_stamp(path / MANIFEST_NAME),
        )
        for path in dirs
    )

    cache_key = str(root)
    cached = _SCAN_CACHE.get(cache_key)
    if cached is not None and cached[0] == stamps:
        return cached[1]

    manifests: list[SkillManifest] = []
    problems: list[SkillProblem] = []
    for path in dirs:
        try:
            manifest = load_manifest(path.name)
        except SkillManifestError as exc:
            problems.append(SkillProblem(skill=path.name, reason=f"元数据无法解析：{exc}"))
            continue
        if manifest is not None:
            manifests.append(manifest)

    result = (manifests, problems)
    _SCAN_CACHE[cache_key] = (stamps, result)
    return result


def missing_artifacts(manifest: SkillManifest) -> list[str]:
    """该技能包当前读不到的必需资源（齐全时为空列表）。

    只看「文件在不在」，不读内容：读内容是加载阶段的事，混在一起会让启动自检变重，
    也让「缺失」和「损坏」两种问题分不清。
    """
    return [
        relative_path
        for relative_path in manifest.artifacts.values()
        if not (manifest.root / relative_path).is_file()
    ]


def audit_skills() -> tuple[list[SkillManifest], list[SkillProblem]]:
    """完整自检：元数据问题 + 资源缺失 + 同一产物被多个技能包声明。

    与 `scan_skills()` 的区别是它**每次都真查资源存在性**，因此不适合放进热路径
    （生成请求里只需要知道用哪个包，不需要体检）。启动自检与 `/health` 用它。
    """
    manifests, problems = scan_skills()

    for manifest in manifests:
        missing = missing_artifacts(manifest)
        if missing:
            problems.append(
                SkillProblem(
                    skill=manifest.name,
                    reason="缺少技能资源：" + "、".join(missing),
                )
            )

    # 同一产物被两个技能包同时声明：行为是确定的（按名字取第一个），
    # 但几乎一定是配置出错，所以报出来而不是让它悄悄生效。
    claimed: dict[str, list[str]] = {}
    for manifest in manifests:
        for kind in manifest.applies_to:
            claimed.setdefault(kind, []).append(manifest.name)
    for kind, names in sorted(claimed.items()):
        if len(names) > 1:
            problems.append(
                SkillProblem(
                    skill="、".join(sorted(names)),
                    reason=f"多个技能包同时声明服务于 {kind}（实际生效：{sorted(names)[0]}）",
                )
            )

    return manifests, problems


def manifest_for_kind(kind: str) -> SkillManifest | None:
    """按产物类型找技能包；没有技能包声明服务于它时返回 `None`。

    命中多个时按技能包名取第一个——这个冲突由 `audit_skills()` 报出，
    这里不重复判定，只保证行为确定。
    """
    manifests, _ = scan_skills()
    for manifest in manifests:
        if kind in manifest.applies_to:
            return manifest
    return None


def load_skill_artifacts(kind: str) -> dict[str, str] | None:
    """按产物类型加载对应技能包的全部资源（语义键 -> 资源全文）。

    这是「按产物取技能资源」的通用入口：`prd_skill` 用的是 PRD 专属的
    `load_prd_skill_artifacts`（带 PRD 专属的日志事件），其余产物
    （`api_docs` / `prompts`）直接走这里，不必各自再写一遍加载逻辑。

    Args:
        kind: 产物类型，与任务类型 `kind` 一致（prd / api_docs / prompts）。

    Returns:
        语义键到资源全文的字典；技能包不在位、元数据损坏或任一资源缺失时返回 `None`，
        由调用方降级到内置提示词——这条降级路径必须能在回执上看见（债务 D8），
        所以这里**不补占位空串**，回执上体现为 `injected_skills` 为空或不含该包。
    """
    try:
        manifest = manifest_for_kind(kind)
    except Exception:  # noqa: BLE001 - 解析期间的任何异常都当成「本次用不上技能包」
        logger.warning(
            "技能包解析异常（kind=%s），本次改用内置提示词生成。",
            kind,
            extra={"event": "skill.load_error", "kind": kind},
        )
        return None

    if manifest is None:
        logger.warning(
            "服务于 %s 的技能包不在位，本次改用内置提示词生成。"
            "生产环境应把 PRDWRIGHT_SKILLS_STRICT 设为 true，让缺失在启动时暴露",
            kind,
            extra={"event": "skill.missing", "kind": kind},
        )
        return None

    missing = missing_artifacts(manifest)
    if missing:
        logger.warning(
            "技能资源缺失（%s）：%s，本次改用内置提示词生成。"
            "生产环境应把 PRDWRIGHT_SKILLS_STRICT 设为 true，让缺失在启动时暴露",
            manifest.name,
            "、".join(missing),
            extra={"event": "skill.assets_missing", "skill": manifest.name, "missing": missing},
        )
        return None

    return {
        key: read_text_cached(manifest.artifact_path(key)) for key in manifest.artifacts
    }


# ---------------------------------------------------------------------------
# 身份与指纹
# ---------------------------------------------------------------------------


def skill_fingerprint(manifest: SkillManifest) -> str:
    """资源内容合成的整体指纹：`sha256(版本 + 各资源的路径与内容)[:12]`。

    改一个字即变，所以它也是「人工维护模板版本号」的自动化替代——不必依赖谁记得改版本。
    `key` 排序后参与摘要，保证同一份内容得到的指纹稳定。

    Raises:
        FileNotFoundError: 任一资源缺失时抛出（调用方请先做 `missing_artifacts`）。
    """
    digest = hashlib.sha256()
    digest.update(manifest.version.encode("utf-8"))
    for key in sorted(manifest.artifacts):
        digest.update(manifest.artifacts[key].encode("utf-8"))
        digest.update(read_text_cached(manifest.artifact_path(key)).encode("utf-8"))
    return digest.hexdigest()[:12]


def describe_manifest(manifest: SkillManifest) -> dict[str, str] | None:
    """把技能包身份打包成 `run_summary.injected_skills` 的一个条目。

    资源不齐全时返回 `None`：此时生成会降级到内置提示词，而「这次到底有没有用上
    技能包」必须能在回执上看得见（债务 D8），因此不返回残缺条目、也不返回占位值。
    """
    if missing_artifacts(manifest):
        return None
    return {
        "name": manifest.name,
        "version": manifest.version,
        "fingerprint": skill_fingerprint(manifest),
    }


def describe_kind(kind: str) -> dict[str, str] | None:
    """按产物类型取技能包身份（没有对应技能包、或它不完整时返回 `None`）。"""
    manifest = manifest_for_kind(kind)
    if manifest is None:
        return None
    return describe_manifest(manifest)


# ---------------------------------------------------------------------------
# 启动自检与缓存清理
# ---------------------------------------------------------------------------


def ensure_skills(*, strict: bool = False) -> tuple[list[SkillManifest], list[SkillProblem]]:
    """启动自检：扫描技能资产并汇总问题；`strict` 为真时存在问题即 fail-fast。

    放在应用 `lifespan` 里跑一遍，部署漏打包 `skills/` 目录就能在**起服务的时候**
    炸出来，而不是等某次生成悄悄退回内置提示词、文档风格变了都没人知道（债务 D8）。

    Args:
        strict: 为真时存在任何问题都抛错中止启动。

    Returns:
        `(可用的技能包, 问题清单)`。清单同时供调用方打启动日志（「这次进程究竟加载了
        哪套资产」），所以返回清单而不是只返回问题，省掉调用方再扫一遍。

    Raises:
        RuntimeError: `strict` 为真且存在问题。
    """
    manifests, problems = audit_skills()
    if problems and strict:
        detail = "；".join(f"{item.skill}：{item.reason}" for item in problems)
        raise RuntimeError(
            f"技能资产自检未通过（共 {len(problems)} 项）：{detail}。"
            f"已解析的 skills 根目录为 {skills_root()}；"
            f"可用环境变量 {SKILLS_DIR_ENV} 指定 skills 根目录，"
            "或在部署产物中打包该目录。不想让启动失败请把 PRDWRIGHT_SKILLS_STRICT 设为 false。"
        )
    return manifests, problems


def clear_caches() -> None:
    """清空全部缓存（资源内容、元数据、扫描结果）。

    正常路径不需要调用它（改文件会让文件戳失效）；测试里用来保证下一次读取真的走盘，
    避免同一份 fixture 在用例之间互相污染。
    """
    _ARTIFACT_CACHE.clear()
    _MANIFEST_CACHE.clear()
    _SCAN_CACHE.clear()
