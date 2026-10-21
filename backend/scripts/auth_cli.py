"""认证运维命令行工具：查看账号、直接建号、重置密码、删除账号、发放邀请码。

用法（在 `backend/` 目录下执行）：

    uv run python scripts/auth_cli.py list
    uv run python scripts/auth_cli.py user alice
    uv run python scripts/auth_cli.py reset-password alice
    uv run python scripts/auth_cli.py delete-user alice
    uv run python scripts/auth_cli.py promote alice
    uv run python scripts/auth_cli.py demote alice
    uv run python scripts/auth_cli.py invite WELCOME --max-uses 5 --expires 2026-12-31

为什么需要它：邀请码会在首次启动时自动播种一条（见 `INVITE_CODE`），
但要发给不同人、限量、限时，就得有个发码入口；同理，运维初始化账号也不该
去走「注册接口 + 邀请码」这条路。

它读写的是服务运行时那个 SQLite 库（`DB_PATH`，缺省 `backend/prdwright.db`），
因此**后端不需要重启**，改动即时生效。

为什么需要一个「任命管理员」的入口
----------------------------------
`INIT_ADMIN_*` 只在**用户表为空**时才创建首个账号（见 `auth_service.ensure_bootstrap`），
所以它对全新部署有效，对「已经有人注册过的库」完全不触发。而任命管理员本身
又需要管理员权限——这是个死锁，只能由运维在库这一层直接打破。`promote` 就是
那把钥匙：**它是唯一不需要已有管理员就能产生管理员的路径**，因此不该被删掉。

本脚本只做参数解析与结果打印，全部业务规则都在 `services/auth_service` 里，
不在这里另写一套校验，也不直接碰 SQL。
"""

from __future__ import annotations

import argparse
import getpass
from datetime import datetime

from backend.core.config import get_settings
from backend.core.db import init_schema
from backend.core.migrations import run_migrations
from backend.repositories.auth_repo import InviteCodeRepository
from backend.services import auth_service


def _read_password(args: argparse.Namespace, prompt: str) -> str:
    """取密码：命令行没给就交互式输入，避免明文留在 shell 历史里。"""
    return args.password or getpass.getpass(prompt)


def _cmd_list(_: argparse.Namespace) -> int:
    """列出用户与邀请码。"""
    print(f"数据库：{get_settings().db_file}\n")

    users = auth_service.list_users()
    print("用户：")
    if not users:
        print("  （暂无用户）")
    for user in users:
        state = "启用" if user["is_active"] else "停用"
        role = "管理员" if user.get("is_admin") else "普通用户"
        print(f"  #{user['id']}  {user['username']}  [{state}/{role}]  创建于 {user['created_at']}")

    codes = InviteCodeRepository.list_all()
    print("\n邀请码：")
    if not codes:
        print("  （暂无邀请码）")
    for code in codes:
        limit = "不限" if code["max_uses"] == 0 else code["max_uses"]
        expiry = code["expires_at"] or "永不过期"
        note = f"  # {code['note']}" if code["note"] else ""
        print(f"  {code['code']}  已用 {code['used_count']}/{limit}  过期：{expiry}{note}")

    return 0


def _cmd_user(args: argparse.Namespace) -> int:
    """不经过邀请码直接建号。"""
    try:
        user = auth_service.create_user_direct(args.username, _read_password(args, "密码："))
    except auth_service.AuthError as exc:
        print(f"失败：{exc.message}")
        return 1
    print(f"已创建用户 #{user.id} {user.username}")
    return 0


def _cmd_reset_password(args: argparse.Namespace) -> int:
    """重置用户密码。"""
    try:
        auth_service.reset_password(args.username, _read_password(args, "新密码："))
    except auth_service.AuthError as exc:
        print(f"失败：{exc.message}")
        return 1
    print(f"已重置 {args.username} 的密码")
    return 0


def _cmd_delete_user(args: argparse.Namespace) -> int:
    """删除用户。其登录会话会一并失效（级联删除），等同于强制下线。

    删号不可恢复，所以默认要二次确认；脚本化调用时用 `--yes` 跳过。
    """
    if not args.yes:
        answer = input(f"确认删除用户 {args.username}？该操作不可恢复 [y/N] ")
        if answer.strip().lower() != "y":
            print("已取消")
            return 0

    try:
        auth_service.delete_user(args.username)
    except auth_service.AuthError as exc:
        print(f"失败：{exc.message}")
        return 1
    print(f"已删除用户 {args.username}（其登录会话一并失效）")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    """把用户提升为管理员。"""
    try:
        user = auth_service.set_admin(args.username, True)
    except auth_service.AuthError as exc:
        print(f"失败：{exc.message}")
        return 1
    print(f"已将 {user.username} 提升为管理员")
    return 0


def _cmd_demote(args: argparse.Namespace) -> int:
    """撤销管理员身份。

    最后一位管理员不会被撤销（服务层会拒绝）——否则系统里再没有人能进后台，
    而那时连「再任命一个」都做不了，只能手工改库。
    """
    try:
        user = auth_service.set_admin(args.username, False)
    except auth_service.AuthError as exc:
        print(f"失败：{exc.message}")
        return 1
    print(f"已撤销 {user.username} 的管理员身份")
    return 0


def _cmd_invite(args: argparse.Namespace) -> int:
    """发放一个邀请码。"""
    if InviteCodeRepository.get(args.code) is not None:
        print(f"失败：邀请码 {args.code} 已存在")
        return 1

    expires_at = None
    if args.expires:
        try:
            expires_at = datetime.strptime(args.expires, "%Y-%m-%d")
        except ValueError:
            print("失败：--expires 需为 YYYY-MM-DD 格式")
            return 1

    try:
        InviteCodeRepository.create(
            args.code,
            max_uses=args.max_uses,
            expires_at=expires_at,
            note=args.note or "",
        )
    except Exception as exc:  # 最常见的是码已存在（主键冲突）
        print(f"失败：{exc}")
        return 1

    limit = "不限次数" if args.max_uses == 0 else f"限 {args.max_uses} 次"
    print(f"已发放邀请码 {args.code}（{limit}，过期：{args.expires or '永不过期'}）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="PRDwright 认证运维工具")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出用户与邀请码").set_defaults(func=_cmd_list)

    user = sub.add_parser("user", help="直接创建用户（不需要邀请码）")
    user.add_argument("username")
    user.add_argument("password", nargs="?", help="省略则交互式输入")
    user.set_defaults(func=_cmd_user)

    reset = sub.add_parser("reset-password", help="重置用户密码")
    reset.add_argument("username")
    reset.add_argument("password", nargs="?", help="省略则交互式输入")
    reset.set_defaults(func=_cmd_reset_password)

    delete = sub.add_parser("delete-user", help="删除用户（其登录会话一并失效）")
    delete.add_argument("username")
    delete.add_argument("--yes", action="store_true", help="跳过二次确认，用于脚本化调用")
    delete.set_defaults(func=_cmd_delete_user)

    promote = sub.add_parser("promote", help="把用户提升为管理员")
    promote.add_argument("username")
    promote.set_defaults(func=_cmd_promote)

    demote = sub.add_parser("demote", help="撤销管理员身份（最后一位管理员不会被撤销）")
    demote.add_argument("username")
    demote.set_defaults(func=_cmd_demote)

    invite = sub.add_parser("invite", help="发放邀请码")
    invite.add_argument("code")
    invite.add_argument("--max-uses", type=int, default=0, help="最多可用次数，0 表示不限（默认）")
    invite.add_argument("--expires", help="过期日期，格式 YYYY-MM-DD；留空表示永不过期")
    invite.add_argument("--note", help="备注，便于以后知道这个码发给谁了")
    invite.set_defaults(func=_cmd_invite)

    return parser


def main() -> int:
    """入口。建表（幂等）**并补齐迁移**后分发子命令。

    必须跑迁移：`init_schema` 只建基线表，对已存在的表是空操作，
    而 `is_admin` 这类后来加的列全靠迁移补。少了这一步，`promote` 在旧库上
    会直接报 `no such column: is_admin`——报错点离原因很远。
    """
    init_schema()
    run_migrations()
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
