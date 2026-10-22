"""主密钥与密钥加解密（单主密钥方案）。

分工
----
- **主密钥**：`backend/secrets/master.key`，32 字节，文件权限 600，已在 .gitignore。
  由 `scripts/keygen/generate_keys.py` 生成；**不进数据库、不进 env**。
- **密钥值**：用主密钥加密后存 `model_providers.value_token`。数据库里只有密文，
  单独拷走数据库（备份 / 导出 / 给人排查）都解不开。

为什么用 Fernet
---------------
Fernet 的密文是**自包含**的：随机数（nonce）+ 密文 + 认证标签都在同一个字符串里。
所以一列 `value_token` 就够，不需要另开 nonce/tag 列，也不会出现「有人改了密文
但没改校验」这种静默降级——篡改会直接抛 `InvalidToken`。

主密钥缺失时的行为
------------------
**明确报错、不自动生成**。自动生成看着友好，实则危险：一旦有人在「密钥已存在但
路径配错」的机器上跑起来，会悄悄造一把新主密钥，旧密文全解不开，而现象只是
「密钥明明配了却报错」。所以启动缺文件时直接告诉你去跑生成脚本。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from backend.core.config import BACKEND_ROOT

logger = logging.getLogger(__name__)

# 路径覆盖只给测试与非常规部署用；正常就是 backend/secrets/master.key。
_MASTER_KEY_PATH_ENV = "PRDWRIGHT_MASTER_KEY_PATH"

# 缓存：(路径, mtime) -> Fernet。mtime 变了就重读，这样轮换主密钥后
# 无需重启也能生效（与配置层「改了下次生成就生效」的口径一致）。
_cache: dict[str, tuple[float, Fernet]] = {}


def master_key_path() -> Path:
    """主密钥文件位置。做成函数是为了测试能替换，而不是直接引用常量。"""
    override = os.environ.get(_MASTER_KEY_PATH_ENV)
    if override:
        return Path(override)
    return Path(BACKEND_ROOT) / "secrets" / "master.key"


def master_key_exists() -> bool:
    return master_key_path().is_file()


def _fernet() -> Fernet:
    """取当前主密钥对应的 Fernet 实例（按 mtime 缓存）。

    文件不存在 / 内容非法时抛 `RuntimeError`，文案带「去跑生成脚本」的指引——
    这条错误会在启动或首次调用模型时冒出来，必须能一眼看懂。
    """
    path = master_key_path()
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise RuntimeError(
            f"找不到主密钥文件：{path}。"
            "请先运行 `uv run python scripts/keygen/generate_keys.py` 生成后再启动。"
        ) from exc

    key = str(path)
    cached = _cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    raw = path.read_bytes().strip()
    try:
        fernet = Fernet(raw)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"主密钥文件内容不是合法的 Fernet 密钥：{path}。"
            "请重新运行生成脚本，或从备份恢复正确的主密钥。"
        ) from exc

    _cache[key] = (mtime, fernet)
    logger.debug("已加载主密钥", extra={"event": "crypto.master_key_loaded", "path": key})
    return fernet


def encrypt_value(plain: str) -> str:
    """把明文密钥加密成可入库的密文。空串原样返回空串（= 未配置）。"""
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_value(token: str) -> str:
    """把密文解回明文。空串返回空串；密文损坏时抛 `RuntimeError`。

    损坏的原因通常只有两类：主密钥被换过（旧密文解不开）、或密文被手工改坏。
    两种情况都不能静默返回空串——那会让「密钥配了却调不通」变成一个无解之谜。
    """
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            "密文无法用当前主密钥解密：主密钥可能已被轮换，或该密文已损坏。"
            "请在管理后台重新录入这条配置的密钥。"
        ) from exc


def is_configured(token: str) -> bool:
    """这条配置是否有一把**可用**的密钥（非空且能被当前主密钥解开）。

    给后台的 `key_present` 与 `/health` 用。只回布尔，绝不回值。
    """
    if not token:
        return False
    try:
        return bool(decrypt_value(token))
    except RuntimeError:
        return False


def reset_cache() -> None:
    """清掉主密钥缓存。测试替换主密钥后调用；生产代码不需要（mtime 会自己变）。"""
    _cache.clear()
