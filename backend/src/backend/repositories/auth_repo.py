"""认证相关的数据仓储：users / auth_sessions / invite_codes 三张表。

日期时间统一按 **UTC 的 ISO 8601 字符串**存储（不带时区后缀，例如
`2026-10-13T05:20:00`）。这样字典序与时间序一致，可以直接在 SQL 里比较大小。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.core.db import connect


def utc_now() -> datetime:
    """当前 UTC 时间（naive，不带时区；库里一律按 UTC 存）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_text() -> str:
    """当前 UTC 时间的 ISO 8601 字符串，用于写入库里的日期字段。"""
    return utc_now().isoformat()


class UserRepository:
    """`users` 表的读写。"""

    @staticmethod
    def create(username: str, password_hash: str, salt: bytes) -> int:
        """新增用户，返回其 id。用户名重复会抛 `sqlite3.IntegrityError`。"""
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, salt, created_at) "
                "VALUES (?, ?, ?, ?)",
                (username, password_hash, salt, utc_now_text()),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    @staticmethod
    def get_by_username(username: str) -> dict[str, Any] | None:
        """按用户名查一条用户记录；不存在返回 None。"""
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def get_by_id(user_id: int) -> dict[str, Any] | None:
        """按 id 查一条用户记录；不存在返回 None。"""
        with connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def count() -> int:
        """用户总数，用于判断是否需要进行初始账号的播种。"""
        with connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            return int(row["n"]) if row else 0

    @staticmethod
    def list_all() -> list[dict[str, Any]]:
        """列出全部用户（不含密码哈希与 salt），供运维查看。"""
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, username, created_at, is_active FROM users ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def update_password(user_id: int, password_hash: str, salt: bytes) -> None:
        """更新密码哈希与 salt（重置密码时用）。"""
        with connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
                (password_hash, salt, user_id),
            )
            conn.commit()

    @staticmethod
    def delete(user_id: int) -> None:
        """删除用户。

        其登录会话由外键的 `ON DELETE CASCADE` 一并清掉——因此删号即刻等同于
        强制下线，不需要再单独清一遍 `auth_sessions`。
        """
        with connect() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()


class AuthSessionRepository:
    """`auth_sessions` 表的读写。

    库里存的是 token 的 **SHA-256 哈希**而不是 token 本身，因此即便数据库文件泄露，
    攻击者也无法拿这些值直接冒充登录态。
    """

    @staticmethod
    def create(token_hash: str, user_id: int, expires_at: datetime) -> None:
        """登记一个会话。"""
        with connect() as conn:
            conn.execute(
                "INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash, user_id, utc_now_text(), expires_at.isoformat()),
            )
            conn.commit()

    @staticmethod
    def fetch_valid(token_hash: str) -> dict[str, Any] | None:
        """取一条**未过期**的会话记录；不存在或已过期返回 None。

        过期判断放在 SQL 条件里，避免把过期会话取出来再在 Python 里丢弃。
        """
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM auth_sessions "
                "WHERE token_hash = ? AND expires_at > ?",
                (token_hash, utc_now_text()),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def delete(token_hash: str) -> None:
        """删除单个会话（登出时用）。"""
        with connect() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))
            conn.commit()

    @staticmethod
    def delete_expired() -> int:
        """清理所有已过期会话，返回清理条数。可定期调用，避免表无限增长。"""
        with connect() as conn:
            cur = conn.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ?", (utc_now_text(),)
            )
            conn.commit()
            return int(cur.rowcount or 0)


class InviteCodeRepository:
    """`invite_codes` 表的读写。"""

    @staticmethod
    def create(code: str, max_uses: int = 0, expires_at: datetime | None = None, note: str = "") -> None:
        """新增一个邀请码。`max_uses=0` 表示不限次数；`expires_at=None` 表示永不过期。"""
        with connect() as conn:
            conn.execute(
                "INSERT INTO invite_codes (code, max_uses, used_count, expires_at, created_at, note) "
                "VALUES (?, ?, 0, ?, ?, ?)",
                (
                    code,
                    max_uses,
                    expires_at.isoformat() if expires_at else None,
                    utc_now_text(),
                    note,
                ),
            )
            conn.commit()

    @staticmethod
    def get(code: str) -> dict[str, Any] | None:
        """按码查记录；不存在返回 None。"""
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM invite_codes WHERE code = ?", (code,)
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def consume(code: str) -> bool:
        """**原子地**占用一次使用额度，成功返回 True。

        「检查额度 + 自增」必须在一条 UPDATE 里完成，靠 `rowcount` 判断是否抢到——
        否则两个请求同时用同一个限量码时会双双通过，导致超发。
        """
        with connect() as conn:
            cur = conn.execute(
                "UPDATE invite_codes SET used_count = used_count + 1 "
                "WHERE code = ? AND (max_uses = 0 OR used_count < max_uses) "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (code, utc_now_text()),
            )
            conn.commit()
            return cur.rowcount == 1

    @staticmethod
    def count() -> int:
        """邀请码总数，用于判断是否需要播种初始码。"""
        with connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM invite_codes").fetchone()
            return int(row["n"]) if row else 0

    @staticmethod
    def list_all() -> list[dict[str, Any]]:
        """列出全部邀请码及其使用情况，供运维查看。"""
        with connect() as conn:
            rows = conn.execute(
                "SELECT code, max_uses, used_count, expires_at, created_at, note "
                "FROM invite_codes ORDER BY created_at, code"
            ).fetchall()
            return [dict(row) for row in rows]
