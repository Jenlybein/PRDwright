"""认证业务服务：注册 / 登录 / 登出 / 当前用户。

职责边界：
- **这里负责业务判断**：密码怎么算、会话多久过期、邀请码是否合法、失败时怎么报错。
- 数据读写一律委托 `repositories/auth_repo`，本模块**不出现 SQL**。
- 登录凭据的表示方式被抽象成 `TokenIssuer`。当前实现是不透明 token + SQLite 会话表；
  将来若确实需要 JWT，只需新增一个实现并在 `get_token_issuer()` 里替换，其余代码零改动。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from backend.core.config import get_settings
from backend.core.db import utc_now
from backend.core.errors import ErrorCode
from backend.core.runtime_config import get_runtime_config
from backend.repositories.auth_repo import (
    AuthSessionRepository,
    InviteCodeRepository,
    LoginFailureRepository,
    UserRepository,
)

# scrypt 参数：n=2**14（16384）是交互式登录场景的常用下限，单次约 16MB 内存，
# 足以让 GPU 批量破解变得不划算。用标准库实现，因此**不需要引入任何新依赖**。
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16

# 用户名 / 密码的基本约束
_USERNAME_MIN = 3
_USERNAME_MAX = 32
_PASSWORD_MIN = 8

# 开发模式（AUTH_ENABLED=false）下所有操作归属的内置用户
_DEV_USERNAME = "dev"


# ---------------------------------------------------------------------------
# 领域对象与错误
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class User:
    """已登录的用户。

    刻意**不包含**密码哈希与 salt——这类敏感字段不该在业务层之间传递。
    """

    id: int
    username: str
    is_active: bool
    created_at: str = ""
    # 是否平台管理员。默认 False 让「取不到这一列」的旧代码路径表现为**无权限**
    # 而不是有权限——权限判断出错时，安全的方向是拒绝。
    is_admin: bool = False


class AuthError(Exception):
    """认证或注册失败。

    错误码**由抛出点直接给定**（默认 `AUTH_FAILED`），路由层原样透传即可。
    刻意不在 `api/` 层做「中文文案 → 错误码」的映射表：文案一改映射就静默失效，
    那正是本次改造要消灭的耦合。

    Attributes:
        message: 可以直接展示给用户看的中文说明。
        code: 机器可读的错误码（`core/errors.py` 的 `ErrorCode`）。
    """

    def __init__(self, message: str, code: ErrorCode = ErrorCode.AUTH_FAILED) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------


def _derive(password: str, salt: bytes) -> str:
    """用 scrypt 派生密钥并返回十六进制字符串。"""
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
    )
    return digest.hex()


def hash_password(password: str) -> tuple[str, bytes]:
    """为新用户生成密码哈希。

    Returns:
        `(十六进制哈希, salt)`，两者都需要存库——salt 每次随机，
        因此相同密码的两个用户哈希值也不同。
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    return _derive(password, salt), salt


def verify_password(password: str, password_hash: str, salt: bytes) -> bool:
    """校验密码是否正确。

    用 `hmac.compare_digest` 做**定长时间比较**，避免比较过程中提前返回
    而泄露「前几位是对的」这类时序信息。
    """
    return hmac.compare_digest(_derive(password, salt), password_hash)


def _sha256(text: str) -> str:
    """对 token 做 SHA-256，库里只存哈希值而不是 token 本身。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 登录凭据：签发 / 校验 / 吊销
# ---------------------------------------------------------------------------


class TokenIssuer(Protocol):
    """登录凭据的抽象。

    把「用什么机制表示登录态」关进一个实现里，业务代码只依赖这三个方法。
    换 JWT 时新增一个实现即可，不需要改动路由与本服务的其他部分。
    """

    def issue(self, user_id: int) -> tuple[str, datetime]:
        """签发凭据（**不动**该用户已有的会话）。

        Returns:
            `(写入 Cookie 的值, 过期时间)`。
        """
        ...

    def replace(self, user_id: int) -> tuple[str, datetime]:
        """签发凭据并**吊销该用户此前的全部会话**。

        单点登录的语义在这里：新登录让旧的立刻失效。删除与插入必须原子完成，
        因此实现把它交给 `AuthSessionRepository.replace_for_user`。

        Returns:
            `(写入 Cookie 的值, 过期时间)`。
        """
        ...

    def verify(self, credential: str) -> int | None:
        """校验凭据，返回用户 id；无效或已过期返回 None。"""
        ...

    def revoke(self, credential: str) -> None:
        """吊销凭据（登出时调用）。"""
        ...


class OpaqueSessionIssuer:
    """不透明 token + SQLite 会话表。

    选择它的理由：可以**即时吊销**（登出即删记录、改密码可强制下线、新登录踢掉旧的），
    而 JWT 在有效期内收不回来。代价是每次校验要查一次库——本项目里这是微秒级开销，
    相比动辄几十秒的 LLM 调用完全可以忽略。
    """

    def __init__(self, ttl_hours: float | None = None) -> None:
        """构造签发器。

        Args:
            ttl_hours: 显式指定有效期时**固定**用它（测试要造「立刻过期」的会话走这条）；
                留空则每次签发时从配置**现读**。

        为什么不在构造时固定有效期
        --------------------------
        签发器是模块级单例（`get_token_issuer`），进程活着就不会重建。若在 `__init__`
        里把小时数转成 `timedelta` 存下来，后台把「会话有效期」改小之后**看起来毫无反应**
        ——配置读了、界面也变了，唯独签出来的会话还是旧时长。改配置要生效，就只能让
        真正用到它的那一刻去读。
        """
        self._fixed_ttl = timedelta(hours=ttl_hours) if ttl_hours is not None else None

    def ttl(self) -> timedelta:
        """当前应当使用的有效期。"""
        if self._fixed_ttl is not None:
            return self._fixed_ttl
        # ⚠️ 必须读 `get_runtime_config().settings`：`get_settings()` 只反映环境变量，
        # 后台改的值存在 system_config 里，走 get_settings 会「改了看起来没反应」。
        return timedelta(hours=get_runtime_config().settings.auth_session_ttl_hours)

    def issue(self, user_id: int) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        expires_at = utc_now() + self.ttl()
        AuthSessionRepository.create(_sha256(token), user_id, expires_at)
        return token, expires_at

    def replace(self, user_id: int) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        expires_at = utc_now() + self.ttl()
        AuthSessionRepository.replace_for_user(_sha256(token), user_id, expires_at)
        return token, expires_at

    def verify(self, credential: str) -> int | None:
        if not credential:
            return None
        row = AuthSessionRepository.fetch_valid(_sha256(credential))
        return int(row["user_id"]) if row else None

    def revoke(self, credential: str) -> None:
        if credential:
            AuthSessionRepository.delete(_sha256(credential))


_issuer: TokenIssuer | None = None


def get_token_issuer() -> TokenIssuer:
    """返回进程内共享的凭据签发器（懒加载单例）。

    单例只承载「用哪种签发方式」，**不承载有效期**——有效期由 `OpaqueSessionIssuer.ttl()`
    每次现读配置，否则后台改了配置也不会对新登录生效。
    """
    global _issuer
    if _issuer is None:
        _issuer = OpaqueSessionIssuer()
    return _issuer


# ---------------------------------------------------------------------------
# 注册 / 登录 / 登出
# ---------------------------------------------------------------------------


def _to_user(row: dict[str, object]) -> User:
    """把库里的行转成领域对象。"""
    return User(
        id=int(row["id"]),  # type: ignore[arg-type]
        username=str(row["username"]),
        is_active=bool(row["is_active"]),
        created_at=str(row.get("created_at", "")),
        is_admin=bool(row.get("is_admin", 0)),
    )


def _validate_new_account(username: str, password: str) -> str:
    """校验新账号的用户名与密码，返回清洗后的用户名。

    抽出来是为了让「注册」与「运维直接建号」走同一套强度要求——
    否则脚本建号就会变成绕过密码策略的后门。
    """
    username = username.strip()
    if not (_USERNAME_MIN <= len(username) <= _USERNAME_MAX):
        raise AuthError(f"用户名长度需在 {_USERNAME_MIN}-{_USERNAME_MAX} 个字符之间")
    if len(password) < _PASSWORD_MIN:
        raise AuthError(f"密码至少需要 {_PASSWORD_MIN} 位")
    return username


def register(username: str, password: str, invite_code: str) -> User:
    """注册新用户。

    顺序很关键：**先查重、再核销邀请码、最后建号**。
    若先核销邀请码再发现用户名重复，用户白亏一次邀请额度。

    Raises:
        AuthError: 用户名或密码不合规、用户名已存在、邀请码无效。
    """
    username = _validate_new_account(username, password)
    if not invite_code.strip():
        raise AuthError("请填写邀请码")

    if UserRepository.get_by_username(username) is not None:
        raise AuthError("该用户名已被注册")

    # 原子核销：限量码并发注册时只有一个请求能抢到额度
    if not InviteCodeRepository.consume(invite_code.strip()):
        raise AuthError("邀请码无效或已用完")

    password_hash, salt = hash_password(password)
    user_id = UserRepository.create(username, password_hash, salt)
    row = UserRepository.get_by_id(user_id)
    if row is None:  # 理论上不会发生，兜底避免返回空用户
        raise AuthError("注册失败，请稍后重试")
    return _to_user(row)


def create_user_direct(username: str, password: str) -> User:
    """**不经过邀请码**直接建号，供运维脚本初始化账号使用。

    与 `register` 的差别只有「跳过邀请码核销」这一处，
    用户名 / 密码的强度校验与重名检查完全一致——否则脚本就成了绕过安全策略的后门。
    """
    username = _validate_new_account(username, password)
    if UserRepository.get_by_username(username) is not None:
        raise AuthError("该用户名已被注册")

    password_hash, salt = hash_password(password)
    user_id = UserRepository.create(username, password_hash, salt)
    row = UserRepository.get_by_id(user_id)
    if row is None:  # 理论上不会发生，兜底避免返回空用户
        raise AuthError("创建用户失败，请稍后重试")
    return _to_user(row)


def get_user(user_id: int) -> User | None:
    """按 id 取用户；不存在返回 None。

    管理员后台按 id 操作账号（列表给的也是 id），需要一个公开入口——
    否则调用方只能自己去碰 `UserRepository` 或 `_to_user`，分层就散了。
    """
    row = UserRepository.get_by_id(user_id)
    return _to_user(row) if row else None


def list_users() -> list[dict]:
    """列出全部用户，供运维工具查看（不含任何凭据字段）。"""
    return UserRepository.list_all()


def delete_user(username: str) -> None:
    """删除用户。

    级联效果：**该用户的所有登录会话一并失效**（外键 `ON DELETE CASCADE`），
    所以删号天然等于强制下线，不必再单独清理会话表。

    Raises:
        AuthError: 用户不存在。
    """
    row = UserRepository.get_by_username(username.strip())
    if row is None:
        raise AuthError("该用户不存在")
    UserRepository.delete(int(row["id"]))


def reset_password(username: str, password: str) -> None:
    """重置指定用户的密码。

    走的是与注册同一套强度校验，避免运维通道成为弱密码后门。

    Raises:
        AuthError: 用户不存在，或新密码不合要求。
    """
    username = username.strip()
    row = UserRepository.get_by_username(username)
    if row is None:
        raise AuthError("该用户不存在")

    _validate_new_account(username, password)
    password_hash, salt = hash_password(password)
    UserRepository.update_password(int(row["id"]), password_hash, salt)


def set_admin(username: str, is_admin: bool) -> User:
    """任命 / 撤销管理员。

    业务规则只有一条，但很关键：**不能撤销最后一位管理员**。
    否则系统里再没有一个人进得了后台，而这个状态**无法自助恢复**——
    任命管理员本身需要管理员权限，只能靠运维直接改库。
    宁可拒绝这次操作并把原因说清楚。

    Raises:
        AuthError: 用户不存在，或这是最后一位管理员。
    """
    row = UserRepository.get_by_username(username.strip())
    if row is None:
        raise AuthError("该用户不存在")

    user_id = int(row["id"])
    # 只有「本来是管理员 + 要撤销 + 撤完就没了」这才拦；任命与重复任命都不受影响
    if not is_admin and bool(row.get("is_admin")) and UserRepository.count_admins() <= 1:
        raise AuthError("这是最后一位管理员，不能撤销——至少保留一位，否则无人能进后台")

    UserRepository.set_is_admin(user_id, is_admin)
    updated = UserRepository.get_by_id(user_id)
    return _to_user(updated) if updated else _to_user(row)


# ---------------------------------------------------------------------------
# 登录失败限流：渐进延时
# ---------------------------------------------------------------------------

# 递增延时的封顶（秒）。8 秒能把在线爆破的吞吐压到每天一万次以内，
# 又不至于让打错密码的真人以为页面卡死。
_LOGIN_DELAY_CAP_SECONDS = 8.0


def _attempt_key(username: str) -> str:
    """失败计数的键：`strip` + **小写**。

    `users.username` 是大小写敏感的（`get_by_username` 精确匹配），所以 `admin`、
    `Admin`、`ADMIN` 是三个不同的查询键。计数若也按原始大小写存，攻击者在三者之间
    轮换就永远触发不了阈值。归一之后它们共用一个计数器，而这不会影响任何合法登录
    ——大小写不对本来也登不进去。
    """
    return username.strip().lower()


def clear_login_failures(username: str) -> int:
    """清掉某用户名的登录失败记录，返回清除条数（删除账号时调用）。

    为什么删除账号必须清
    --------------------
    失败计数按**用户名**存而不是按 `user_id`（登录时还没有 id——用户可能压根不存在，
    而「不存在的用户名也要计数」正是防枚举的关键）。代价是它不随账号级联删除：
    删掉一个失败过 5 次的 `alice`，再注册一个同名 `alice`，新账号一上来就被延时 2 秒，
    继承了一笔它没欠过的账。

    归一化走 `_attempt_key`，与写计数时同一个键——否则删的时候清的是另一种拼写，
    行还留着。
    """
    return LoginFailureRepository.clear(_attempt_key(username))


def _window_start() -> str:
    """统计窗口的起点（UTC ISO 文本；字典序即时间序，可直接在 SQL 里比大小）。"""
    minutes = get_runtime_config().settings.login_failure_window_minutes
    return (utc_now() - timedelta(minutes=minutes)).isoformat()


def login_delay(username: str) -> float:
    """本次登录应当**先等几秒**再校验（渐进延时）。

    为什么是延时而不是锁账号
    ------------------------
    硬锁（失败 N 次后拒绝该账号登录一段时间）挡得更死，但它有一个致命副作用：
    攻击者只要知道用户名，故意输错几次就能把这个人**锁在门外**——用防爆破的功能
    做出了拒绝服务的效果。延时没有这个问题：别人最多让你每次登录慢 8 秒，
    正确密码依然进得去。

    阈值与递增
    ----------
    窗口内失败次数 `< login_max_attempts` → 不延时。
    达到阈值后每多失败一次翻一倍，封顶 `_LOGIN_DELAY_CAP_SECONDS`：

        5 次（阈值 5）→ 0s，6 次 → 2s，7 次 → 4s，8 次及以上 → 8s

    ⚠️ 本函数只**算**不**睡**
    -------------------------
    `login` 及其调用链是同步代码，跑在事件循环线程里。在这里 `time.sleep` 会把
    **整个进程**卡住（不只是这一条请求，全站的 SSE 流都会停）。真正的等待必须由
    路由层用 `await asyncio.sleep()` 落实。

    Args:
        username: 用户提交的原始用户名（内部自行归一）。

    Returns:
        应当等待的秒数；`login_max_attempts <= 0`（关闭限流）时恒为 0。
    """
    limit = get_runtime_config().settings.login_max_attempts
    if limit <= 0:
        return 0.0
    failures = LoginFailureRepository.count_since(_attempt_key(username), _window_start())
    if failures < limit:
        return 0.0
    return float(min(2 ** (failures - limit + 1), _LOGIN_DELAY_CAP_SECONDS))


def _record_failure(username: str, ip: str) -> None:
    """记一次登录失败。"""
    LoginFailureRepository.record(_attempt_key(username), ip)


def _on_login_success(username: str) -> None:
    """登录成功后清账。

    三件事：
    1. 删掉该用户名的失败记录——不清的话「昨天错了 5 次」会让今天第一次登录就白等 2 秒。
    2. 删掉窗口外的陈旧计数（已经不影响任何判定，留着只是让表变大）。
    3. 清掉过期会话。`AuthSessionRepository.delete_expired` 之前**从来没被调用过**
       （定义了但没人调，属于死代码），会话表只增不减；挂在登录成功这条必经且低频的
       路径上，不需要额外的定时任务。
    """
    LoginFailureRepository.clear(_attempt_key(username))
    LoginFailureRepository.purge_before(_window_start())
    AuthSessionRepository.delete_expired()


def login(
    username: str,
    password: str,
    ip: str = "",
    *,
    require_admin: bool = False,
) -> tuple[User, str, datetime]:
    """校验账号密码并签发会话。

    注意五个细节：
    1. 用户不存在与密码错误**返回同一句提示**，避免泄露哪些用户名已注册（账号枚举）。
    2. 用户不存在时也执行一次等价的 scrypt 运算再返回，避免「响应更快」
       从时序上暴露该用户名不存在。
    3. 签发走 `replace()` 而不是 `issue()`：这是**单点登录**——登录成功即吊销该用户
       此前的全部会话（见 ADR-024）。代价是多设备会互相踢下线，这是明确接受的风险；
       好处是不需要再维护一套「会话版本号 + 乐观锁」来对付并发登录，少一处状态就少一类
       不一致。
    4. **失败要计数，不存在的用户名也计数**（见 `_record_failure`）。若只给真实账号
       计数，「有没有延时」本身就成了新的枚举信号，把第 1、2 条白做了。
    5. `require_admin=True` 时，**密码正确但账号不是管理员**一律当作 `账号不存在`
       （见 ADR-049）。从管理后台这个入口看过去，非管理员账号本来就不存在于它的世界里。

    ⚠️ 渐进延时不在本函数里落实：调用方必须先问 `login_delay()` 并自行 `await`
    相应时长，否则要么不生效、要么把事件循环卡死。

    Args:
        username: 用户名。
        password: 明文密码（仅在内存中与库里的哈希比对）。
        ip: 来源 IP，只用于记录失败现场，不参与判定。
        require_admin: 是否要求管理员身份（管理后台的登录入口传 `True`）。

    Returns:
        `(用户, 写入 Cookie 的凭据, 过期时间)`。

    Raises:
        AuthError: 账号或密码错误、账号已停用、账号不是管理员。
    """
    username = username.strip()
    row = UserRepository.get_by_username(username)

    if row is None:
        # 用固定 salt 做一次等价运算，抹平与「用户存在」时的耗时差异
        _derive(password, b"prdwright-dummy-salt")
        _record_failure(username, ip)
        raise AuthError("用户名或密码错误")

    if not verify_password(password, str(row["password_hash"]), bytes(row["salt"])):
        _record_failure(username, ip)
        raise AuthError("用户名或密码错误")
    if not row["is_active"]:
        # 密码是对的，只是账号被停用：这不是一次「猜错」，不计入失败次数
        raise AuthError("该账号已被停用")
    if require_admin and not bool(row.get("is_admin")):
        # 密码是对的，但不是管理员。**必须查完密码再说这句话**，理由有两条：
        #
        # 1. 反过来（先查 is_admin）会让「这个用户名是不是管理员」在不知道密码的情况下
        #    就能被问出来——那等于给出一条管理员名单的探测通道；
        # 2. 现在是「凭据本来有效、只是不该从这里进」，与 brute force 无关，
        #    不计失败次数（同「账号已被停用」），也不触发单点登录的会话替换。
        #
        # 文案取「账号不存在」而不是「无权限」：管理后台对外只承认管理员账号存在
        # （ADR-049）。这句话在**不签发任何会话**的前提下返回，所以它不改变主站登录态。
        raise AuthError("账号不存在")

    _on_login_success(username)
    user = _to_user(row)
    credential, expires_at = get_token_issuer().replace(user.id)
    return user, credential, expires_at


def logout(credential: str) -> None:
    """登出：吊销当前凭据。"""
    get_token_issuer().revoke(credential)


def current_user(credential: str) -> User | None:
    """由凭据解析当前用户；无效或过期返回 None。"""
    user_id = get_token_issuer().verify(credential)
    if user_id is None:
        return None
    row = UserRepository.get_by_id(user_id)
    return _to_user(row) if row else None


# ---------------------------------------------------------------------------
# 初始化：邀请码播种、初始管理员、开发模式用户
# ---------------------------------------------------------------------------


def ensure_bootstrap() -> None:
    """启动时补齐初始数据，幂等。

    两件事：
    1. 邀请码表为空时，播种 `.env` 里配置的 `INVITE_CODE`（不限次数、不过期）。
    2. 用户表为空且配置了 `INIT_ADMIN_USER`/`INIT_ADMIN_PASSWORD` 时，创建首个管理员。
       这样第一次启动就能登录，不必先写脚本加用户。
    """
    settings = get_settings()

    if InviteCodeRepository.count() == 0 and settings.invite_code:
        InviteCodeRepository.create(
            settings.invite_code,
            max_uses=0,
            expires_at=None,
            note="初始邀请码（由 INVITE_CODE 自动播种）",
        )

    if UserRepository.count() == 0 and settings.init_admin_user and settings.init_admin_password:
        password_hash, salt = hash_password(settings.init_admin_password)
        # 自举出来的首个账号**直接是管理员**：否则全新部署后没有任何人能进后台，
        # 而任命管理员本身又需要管理员权限——那是个死锁。
        UserRepository.create(settings.init_admin_user, password_hash, salt, is_admin=True)


def ensure_dev_user() -> User:
    """开发模式（`AUTH_ENABLED=false`）下的内置用户。

    刻意保留一个**真实的用户记录**，而不是让业务数据的 user_id 置空——
    这样数据模型始终一致（user_id 恒 NOT NULL），将来开启鉴权时不需要迁移或补数据。
    该用户密码是随机值，无法用于登录。
    """
    row = UserRepository.get_by_username(_DEV_USERNAME)
    if row is not None:
        return _to_user(row)

    password_hash, salt = hash_password(secrets.token_urlsafe(32))
    user_id = UserRepository.create(_DEV_USERNAME, password_hash, salt)
    created = UserRepository.get_by_id(user_id)
    return _to_user(created) if created else User(id=user_id, username=_DEV_USERNAME, is_active=True)
