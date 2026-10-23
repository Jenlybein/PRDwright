"""认证相关的数据仓储：users / auth_sessions / invite_codes / login_failures 四张表。

每个类都**绑定一个 `Session`**（`__init__(self, session)`）：仓库只负责「把这四张表
翻译成对象」，事务边界一律由服务层的 `session_scope()` 决定。仓库里没有 `connect()`、
没有 `commit()`、没有 SQL 字符串——这三条由 `tests/test_architecture_guards.py` 的
C1/C2/C3 静态守住。

日期时间统一按 **UTC 的 ISO 8601 字符串**存储（不带时区后缀，例如
`2026-10-13T05:20:00`）。这样字典序与时间序一致，可以直接在 SQL 里比较大小。
时间文本的来源是 `core/clock.py`，不要在别处自己 `isoformat()`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlmodel import Session

from backend.core.clock import utc_now_text
from backend.core.models import AuthSession, InviteCode, LoginFailure
from backend.core.models import User as UserModel


@dataclass(frozen=True, slots=True)
class UserSummary:
    """用户列表的一行。

    刻意**不含** `password_hash` / `salt`：`list_all` 是给运维看的名单，
    曾经用 `SELECT *` 的写法把哈希也一起带出去过——只要有一处把行原样塞进响应，
    凭据就泄露了。这里用投影把「能带出去的字段」定死。
    """

    id: int
    username: str
    created_at: str
    is_active: bool
    is_admin: bool

    def as_dict(self) -> dict[str, Any]:
        """转成与旧接口一致的扁平字典（API 层与 CLI 都按这个形状取字段）。"""
        return {
            "id": self.id,
            "username": self.username,
            "created_at": self.created_at,
            "is_active": self.is_active,
            "is_admin": self.is_admin,
        }


class UserRepository:
    """`users` 表的读写。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self, username: str, password_hash: str, salt: bytes, is_admin: bool = False
    ) -> UserModel:
        """新增用户并返回该行。

        返回整行而不是 id：调用方（`register` / 建号脚本）拿到 id 之后立刻还要读一次
        用户名与状态，返回对象就把那一次多余的查询省掉了。

        Args:
            is_admin: 是否建为管理员。**默认 False**——新建账号不带任何权限，
                管理员身份只能来自初始账号的自举或运维显式任命，
                避免「建号」这个动作变成顺手提权的通道。

        Raises:
            sqlalchemy.exc.IntegrityError: 用户名重复（唯一约束在库里，不在应用层判）。
        """
        user = UserModel(
            username=username,
            password_hash=password_hash,
            salt=salt,
            created_at=utc_now_text(),
            is_active=True,
            is_admin=is_admin,
        )
        self.session.add(user)
        # flush 而不是 commit：拿到自增 id，但事务仍然归调用方。
        # 这也是 `lastrowid` 的正规替代——SQLAlchemy 在 flush 时把主键回填到对象上。
        self.session.flush()
        return user

    def get_by_username(self, username: str) -> UserModel | None:
        """按用户名查一条用户记录；不存在返回 None。"""
        return self.session.execute(select(UserModel).where(UserModel.username == username)).scalars().first()

    def get_by_id(self, user_id: int) -> UserModel | None:
        """按 id 查一条用户记录；不存在返回 None。"""
        return self.session.get(UserModel, user_id)

    def count(self) -> int:
        """用户总数，用于判断是否需要进行初始账号的播种。"""
        return int(
            self.session.execute(select(func.count()).select_from(UserModel)).scalar_one()
        )

    def list_all(self) -> list[UserSummary]:
        """列出全部用户（不含密码哈希与 salt），供运维查看。"""
        rows = self.session.execute(
            select(
                UserModel.id,
                UserModel.username,
                UserModel.created_at,
                UserModel.is_active,
                UserModel.is_admin,
            ).order_by(UserModel.id)
        ).all()
        return [
            UserSummary(
                id=int(row[0] or 0),
                username=str(row[1]),
                created_at=str(row[2] or ""),
                is_active=bool(row[3]),
                is_admin=bool(row[4]),
            )
            for row in rows
        ]

    def set_is_admin(self, user_id: int, is_admin: bool) -> None:
        """设置管理员标记（任命 / 撤销管理员的落点）。

        只改这一列、不碰其它字段：撤销管理员**不等于**停用账号，
        那个人照样能正常登录使用产品，只是进不了后台。
        """
        self.session.execute(
            update(UserModel).where(UserModel.id == user_id).values(is_admin=is_admin)
        )

    def count_admins(self) -> int:
        """管理员人数，用于守住「系统至少留一位管理员」这条不变式。"""
        return int(
            self.session.execute(
                select(func.count()).select_from(UserModel).where(UserModel.is_admin.is_(True))
            ).scalar_one()
        )

    def set_active(self, user_id: int, is_active: bool) -> None:
        """停用 / 启用账号。

        停用**不等于**删号：数据全留着，只是这个人登不进来了（`login` 已判
        `is_active`）。因此它是可逆的，而删号不可逆——后台把这两件事分开呈现，
        管理员才不会把「暂时封禁」误操作成「永久清除」。
        """
        self.session.execute(
            update(UserModel).where(UserModel.id == user_id).values(is_active=is_active)
        )

    def update_password(self, user_id: int, password_hash: str, salt: bytes) -> None:
        """更新密码哈希与 salt（重置密码时用）。"""
        self.session.execute(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(password_hash=password_hash, salt=salt)
        )

    def delete(self, user_id: int) -> None:
        """删除用户。

        其登录会话由外键的 `ON DELETE CASCADE` 一并清掉——因此删号即刻等同于
        强制下线，不需要再单独清一遍 `auth_sessions`。级联能生效的前提是连接上
        开着 `PRAGMA foreign_keys=ON`，那一条由 Engine 的 connect 事件统一设置。
        """
        self.session.execute(delete(UserModel).where(UserModel.id == user_id))


class AuthSessionRepository:
    """`auth_sessions` 表的读写。

    库里存的是 token 的 **SHA-256 哈希**而不是 token 本身，因此即便数据库文件泄露，
    攻击者也无法拿这些值直接冒充登录态。
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, token_hash: str, user_id: int, expires_at: datetime) -> None:
        """登记一个会话。"""
        self.session.add(
            AuthSession(
                token_hash=token_hash,
                user_id=user_id,
                created_at=utc_now_text(),
                expires_at=expires_at.isoformat(),
            )
        )

    def replace_for_user(self, token_hash: str, user_id: int, expires_at: datetime) -> None:
        """**吊销该用户的全部旧会话**并登记新会话（单点登录的落点）。

        两步必须在**同一条事务**里完成：

        - 先删后插：中间不留「旧会话已失效、新会话还没立」的窗口。分两次提交的话，
          任何一步失败都会把用户留在「刚登录成功却判定未登录」的状态里。
        - 插失败（如哈希撞上唯一约束）时整笔回滚，旧会话照旧可用。

        这正是「一次业务操作 = 一个事务」的直接受益点：以前要靠 `if owned:` 把两条语句
        手工绑在一起，现在只要不写提交语句，它们天然同生共死。

        刻意**不按过期时间挑着删**：单点登录的语义是「新登录让旧的立刻失效」，
        留着一条还没过期的旧会话就等于留着一个可用的第二登录态。

        Args:
            token_hash: 新会话 token 的 SHA-256 哈希。
            user_id: 会话所属用户。
            expires_at: 新会话的过期时间。
        """
        self.session.execute(
            delete(AuthSession).where(AuthSession.user_id == user_id)
        )
        self.create(token_hash, user_id, expires_at)

    def fetch_valid(self, token_hash: str) -> AuthSession | None:
        """取一条**未过期**的会话记录；不存在或已过期返回 None。

        过期判断放在 SQL 条件里，避免把过期会话取出来再在 Python 里丢弃。
        """
        return self.session.execute(select(AuthSession).where(
                AuthSession.token_hash == token_hash,
                AuthSession.expires_at > utc_now_text(),
            )).scalars().first()

    def list_by_user(self, user_id: int) -> list[AuthSession]:
        """列出某用户的全部会话记录（诊断用）。

        单点登录之后，「一个用户在库里应当**至多一条**会话」本身就是一条可核对的不变量，
        出问题时（例如出现两条）这个方法是第一手证据，因此把它做成公开的读入口，
        而不是让排查者临时去写 SQL。

        Args:
            user_id: 用户 id。

        Returns:
            该用户的会话行列表，按创建时间排序。
        """
        return list(self.session.execute(select(AuthSession)
                .where(AuthSession.user_id == user_id)
                .order_by(AuthSession.created_at)).scalars().all())

    def delete(self, token_hash: str) -> None:
        """删除单个会话（登出时用）。"""
        self.session.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash))

    def delete_expired(self) -> int:
        """清理所有已过期会话，返回清理条数。可定期调用，避免表无限增长。"""
        result = self.session.execute(
            delete(AuthSession).where(AuthSession.expires_at <= utc_now_text())
        )
        return int(result.rowcount or 0)

    def delete_by_user(self, user_id: int) -> int:
        """吊销某用户的全部会话，返回吊销条数（管理员「强制下线」的落点）。

        单点登录（`replace_for_user`）决定了正常情况下每人**至多一条**会话，
        所以返回值通常是 0 或 1：0 表示他本来就没登录，1 表示确实踢掉了一条。
        把条数返回出去，管理员才分得清「踢成功了」与「他压根没在线」，
        否则两种情况的界面反馈一模一样。

        删掉之后该 token 立刻失效：`fetch_valid` 查不到记录即判定未登录。
        """
        result = self.session.execute(
            delete(AuthSession).where(AuthSession.user_id == user_id)
        )
        return int(result.rowcount or 0)


class LoginFailureRepository:
    """`login_failures` 表的读写：登录失败计数，供渐进延时使用。

    只记失败、不记成功——成功登录时整批删掉，因此「行数」本身就是
    「尚未清账的失败次数」。这样就不需要 `ok` 列，也不必在查询里过滤。

    `username` 一律由调用方（`auth_service`）按小写归一后传入：归一放在服务层
    而不是这里，是因为「按什么键计数」属于策略，而仓储只负责存取。
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, username: str, ip: str) -> None:
        """记一次失败。

        Args:
            username: **已归一**（strip + lower）的用户名。
            ip: 来源 IP，仅记录供排查，不参与判定。
        """
        self.session.add(
            LoginFailure(username=username, ip=ip, created_at=utc_now_text())
        )

    def count_since(self, username: str, since: str) -> int:
        """数某用户名在 `since` 之后的失败次数（`since` 为 ISO 文本，字典序即时间序）。"""
        return int(
            self.session.execute(
                select(func.count())
                .select_from(LoginFailure)
                .where(
                    LoginFailure.username == username,
                    LoginFailure.created_at > since,
                )
            ).scalar_one()
        )

    def clear(self, username: str) -> int:
        """清掉某用户名的全部失败记录（登录成功时调用），返回清除条数。"""
        result = self.session.execute(
            delete(LoginFailure).where(LoginFailure.username == username)
        )
        return int(result.rowcount or 0)

    def purge_before(self, cutoff: str) -> int:
        """删掉 `cutoff` 之前的记录，返回删除条数。

        窗口外的失败已经不影响任何判定（`count_since` 只数窗口内），留着只会让表
        无限增长。由登录链路顺手调用，不需要额外的定时任务。
        """
        result = self.session.execute(
            delete(LoginFailure).where(LoginFailure.created_at < cutoff)
        )
        return int(result.rowcount or 0)


class InviteCodeRepository:
    """`invite_codes` 表的读写。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        code: str,
        max_uses: int = 0,
        expires_at: datetime | None = None,
        note: str = "",
    ) -> None:
        """新增一个邀请码。`max_uses=0` 表示不限次数；`expires_at=None` 表示永不过期。"""
        self.session.add(
            InviteCode(
                code=code,
                max_uses=max_uses,
                used_count=0,
                expires_at=expires_at.isoformat() if expires_at else None,
                created_at=utc_now_text(),
                note=note,
            )
        )

    def get(self, code: str) -> InviteCode | None:
        """按码查记录；不存在返回 None。"""
        return self.session.get(InviteCode, code)

    def consume(self, code: str) -> bool:
        """**原子地**占用一次使用额度，成功返回 True。

        「检查额度 + 自增」必须在一条 UPDATE 里完成，靠 `rowcount` 判断是否抢到——
        否则两个请求同时用同一个限量码时会双双通过，导致超发。

        这一条不能改写成「先 get 再改对象属性」：那是读-改-写，两个并发事务都会
        读到同一个 `used_count`，于是都认为自己还有额度。`rowcount` 是这里唯一
        可信的判据。
        """
        result = self.session.execute(
            update(InviteCode)
            .where(
                InviteCode.code == code,
                or_(InviteCode.max_uses == 0, InviteCode.used_count < InviteCode.max_uses),
                or_(InviteCode.expires_at.is_(None), InviteCode.expires_at > utc_now_text()),
            )
            .values(used_count=InviteCode.used_count + 1)
        )
        return int(result.rowcount or 0) == 1

    def count(self) -> int:
        """邀请码总数，用于判断是否需要播种初始码。"""
        return int(
            self.session.execute(select(func.count()).select_from(InviteCode)).scalar_one()
        )

    def list_all(self) -> list[InviteCode]:
        """列出全部邀请码及其使用情况，供运维查看。"""
        return list(self.session.execute(select(InviteCode).order_by(InviteCode.created_at, InviteCode.code)).scalars().all())

    def delete(self, code: str) -> bool:
        """删除一个邀请码，返回是否真的删掉了。

        只删**码本身**，不动已经注册的用户——那些账号是靠码进来的，但进来之后
        就与码无关了。删码影响的是「后面的人还能不能用它注册」，不是「已经注册的人」。
        """
        result = self.session.execute(delete(InviteCode).where(InviteCode.code == code))
        return int(result.rowcount or 0) == 1
