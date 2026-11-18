"""一次性修整（不进版本库）：把摘要里被塞进 `platform` 的非平台事实拆回 `technical_constraints`。

背景：`SUMMARY_SYNC_SYSTEM_PROMPT` 的收纳口径曾写着「是否云端保存 / 是否要登录 / 部署方式…
并进 platform」，于是对话里这些事实被并进了那个字段（线上实见
``platform = "网页应用；免登录；浏览器本地存储，自动保存草稿"``）。它一路污染到
PRD 的平台小节、接口文档编造的平台枚举、提示词的输入契约。提示词已经修好，但**存量摘要**
还带着旧值——这个脚本只修摘要，**不动任何正文、不动版本链**。

做法：把 `platform` 按分隔符切成段，逐段归类——
载体词留在 `platform`，登录类进 `technical_constraints.auth`，存储类进 `.storage`，
部署类进 `.deployment`，其余进 `.other`（没有哪一类就不写）。
幂等：切开后平台段全是载体词时跳过。

用法（在 api 容器内跑，连的是同一个库）：
    docker exec prdwright-api-1 python /tmp/repair_summary_platform.py --dry-run
    docker exec prdwright-api-1 python /tmp/repair_summary_platform.py --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from backend.core.engine import session_scope

# 分隔符：中文分号 / 顿号 / 逗号 / 加号 / 英文分号
_SPLIT = re.compile(r"[;；、,，+]+")

# 载体词：这些段留在 `platform`
_PLATFORM_WORDS = (
    "网页应用", "网页", "web", "h5", "移动app", "app", "微信小程序", "小程序",
    "桌面客户端", "桌面", "纯api", "api服务", "ai应用", "智能体", "小程序端",
)
_AUTH_WORDS = ("登录", "免登录", "认证", "账号", "会话", "token")
_STORAGE_WORDS = ("存储", "本地", "云端", "持久化", "保存", "草稿", "落库")
_DEPLOY_WORDS = ("部署", "托管", "服务器", "docker", "自建")


def _classify(segment: str) -> str:
    """把平台字段里的一段归类：`platform` / `auth` / `storage` / `deployment` / `other`。"""
    text = segment.strip().lower()
    if not text:
        return "empty"
    if any(word in text for word in _PLATFORM_WORDS):
        return "platform"
    if any(word in text for word in _AUTH_WORDS):
        return "auth"
    if any(word in text for word in _STORAGE_WORDS):
        return "storage"
    if any(word in text for word in _DEPLOY_WORDS):
        return "deployment"
    return "other"


def _append(existing: str, value: str) -> str:
    """把一条事实并入已有的约束文本（已有同文时原样返回）。"""
    existing = (existing or "").strip().rstrip("；;。")
    if not existing:
        return value
    if value in existing:
        return existing
    return f"{existing}；{value}"


def repair(summary: dict) -> tuple[dict, bool]:
    """返回（修好的摘要, 是否改动）。不修改入参。"""
    platform = str(summary.get("platform") or "")
    if not platform.strip():
        return summary, False

    kept: list[str] = []
    buckets: dict[str, list[str]] = {"auth": [], "storage": [], "deployment": [], "other": []}
    for segment in _SPLIT.split(platform):
        kind = _classify(segment)
        if kind == "empty":
            continue
        if kind == "platform":
            kept.append(segment.strip())
        else:
            buckets[kind].append(segment.strip())

    if not any(buckets.values()):
        return summary, False

    fixed = dict(summary)
    fixed["platform"] = "、".join(kept) if kept else "网页应用"

    constraints = fixed.get("technical_constraints")
    constraints = dict(constraints) if isinstance(constraints, dict) else {}
    merged_any = False
    for key, values in buckets.items():
        if not values:
            continue
        merged_any = True
        constraints[key] = _append(str(constraints.get(key, "")), "；".join(values))
    if merged_any:
        fixed["technical_constraints"] = constraints
    return fixed, True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真的写库（缺省只预览）")
    args = parser.parse_args()

    from sqlalchemy import select

    from backend.core.models import Solution

    repaired = 0
    skipped = 0
    with session_scope() as session:
        solutions = session.scalars(select(Solution)).all()
        for solution in solutions:
            raw = solution.summary_json
            if not raw:
                skipped += 1
                continue
            try:
                summary = json.loads(raw)
            except (TypeError, ValueError):
                skipped += 1
                continue
            if not isinstance(summary, dict):
                skipped += 1
                continue
            fixed, changed = repair(summary)
            if not changed:
                skipped += 1
                continue
            print(
                f"[solution {solution.id}] platform: {summary.get('platform')!r} → "
                f"{fixed.get('platform')!r}"
            )
            print(f"    technical_constraints: {fixed.get('technical_constraints')}")
            if args.apply:
                solution.summary_json = json.dumps(fixed, ensure_ascii=False)
                session.add(solution)
                repaired += 1

    print(f"完成：修整 {repaired} 份，跳过 {skipped} 份（--apply 才真的写库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
