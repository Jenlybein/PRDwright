"""生成 / 轮换主密钥。

用法（在 backend 目录下跑，保证依赖可用）::

    cd backend
    uv run python ../scripts/keygen/generate_keys.py            # 首次生成
    uv run python ../scripts/keygen/generate_keys.py --rotate   # 轮换主密钥

两条铁律
--------
1. **已存在就拒绝生成**（除非显式 `--rotate`）。盲目覆盖会把旧密文全部变成
   解不开的乱码，而现象只是「密钥明明配了却报错」。
2. **轮换时先改库、最后才换文件**。中途崩溃时旧主密钥还在，仍可恢复；
   反过来先换文件再改库，一旦中断就是不可逆的数据丢失。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 让脚本在「仓库根目录 / backend 目录」两种 cwd 下都能 import 到 backend 包。
REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = REPO_ROOT / "backend" / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402


def _target_path() -> Path:
    # 走与运行时同一个入口，避免「脚本写这里、服务读那里」的分叉。
    from backend.core.crypto import master_key_path

    return master_key_path()


def _write_key(path: Path, key: bytes) -> None:
    """原子写入并尽量收紧权限（Windows 上 chmod 语义有限，失败不阻断）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(key)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def generate() -> int:
    path = _target_path()
    if path.exists():
        print(f"主密钥已存在，拒绝覆盖：{path}")
        print("要换主密钥请用 `--rotate`（它会先重加密库里的密钥，再替换文件）。")
        return 1
    _write_key(path, Fernet.generate_key())
    print(f"已生成主密钥：{path}")
    print("提醒：该文件必须在 .gitignore 中；备份时不要与数据库放进同一个包。")
    return 0


def rotate() -> int:
    from backend.core import crypto
    from backend.core.db import connect, utc_now_text

    path = _target_path()
    if not path.exists():
        print(f"找不到现有主密钥：{path}。首次使用请不加 --rotate 直接生成。")
        return 1

    old_key = path.read_bytes().strip()
    try:
        old = Fernet(old_key)
    except (ValueError, TypeError):
        print(f"现有主密钥不是合法 Fernet 密钥：{path}")
        return 1

    new_key = Fernet.generate_key()
    new = Fernet(new_key)

    with connect() as conn:
        rows = conn.execute(
            "SELECT id, value_token FROM model_providers WHERE value_token != ''"
        ).fetchall()

    updates: list[tuple[str, str, str]] = []
    for row in rows:
        provider_id = str(row["id"])
        token = str(row["value_token"])
        try:
            plain = old.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError):
            # 有解不开的就整体中止：继续换主密钥只会让那些行永久报废。
            print(f"配置 {provider_id} 的密文无法用当前主密钥解开，已中止，未做任何修改。")
            return 1
        updates.append((new.encrypt(plain.encode("utf-8")).decode("ascii"), provider_id))

    now = utc_now_text()
    with connect() as conn:
        conn.executemany(
            "UPDATE model_providers SET value_token = ?, updated_at = ? WHERE id = ?",
            [(token, now, provider_id) for token, provider_id in updates],
        )
        conn.commit()

    # 最后一步才替换主密钥文件——顺序是安全的关键。
    _write_key(path, new_key)
    crypto.reset_cache()
    print(f"已轮换主密钥并重加密 {len(updates)} 条配置的密钥：{path}")
    print("提醒：轮换前的数据库备份已无法用新主密钥解密；如需恢复旧备份，请离线保管旧主密钥。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成或轮换 PRDwright 主密钥")
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="轮换主密钥：先重加密库里的所有密钥，最后替换 master.key",
    )
    args = parser.parse_args()
    return rotate() if args.rotate else generate()


if __name__ == "__main__":
    raise SystemExit(main())
