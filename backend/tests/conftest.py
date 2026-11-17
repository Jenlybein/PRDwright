"""pytest 公共夹具。

认证相关的测试有一个硬性前提：**每个用例都要用独立的数据库**。
否则用例之间会互相看见对方注册的用户、核销掉的邀请码，测试结果随执行顺序漂移。

配置是 `lru_cache` 单例，所以这里不是「改环境变量」就完事——
必须同时 `cache_clear()`，让下一次 `get_settings()` 重新读取。

## 建库：每 worker 一个模板库，每用例 `CREATE DATABASE … TEMPLATE`（#77）

每个用例都跑一次完整的 `alembic upgrade head` 会白花十几秒（全量 1000+ 用例 × 几百毫秒）。
所以这里改成**每个进程（= 每个 xdist worker）建一次模板库**（还是走 `prepare_schema`，
与真实启动同一条路），之后每个用例只做一次 `CREATE DATABASE … TEMPLATE`——派生出来的
是「已经接管好的库」，`client` 夹具的 lifespan 再调 `prepare_schema` 时走的是
`upgrade head` 空转，正好也是真实部署里最常见的那条分支。

**为什么不是「换一份库文件」**：SQLite 靠拷贝库文件隔离数据，Postgres 没有等价物。
`CREATE DATABASE … TEMPLATE` 是它自己的「拷一份」——派生库拿到的是同一个结构、
零数据，用例结束整库删掉。**模板库只有结构、没有数据**，这是「用例之间数据互不可见」的根。

## 集成库从哪来（⚠️ 地址不带库名）

`PRDWRIGHT_TEST_POSTGRES_URL` 给的是**维护库地址、不带库名**：

```
postgresql+psycopg://postgres:postgres@127.0.0.1:5433
```

用例自己往后面拼 `/<库名>` 建库。缺省值就是这个本地集成库；**没有 Postgres 时
本夹具直接让用例失败**（带「设哪个变量 / 本地该起在哪」的提示），绝不静默跳过——
静默绿比红更难发现。
"""

from __future__ import annotations

import pytest

import dbsupport
from backend.core.config import get_settings


def _reset_runtime_state() -> None:
    """清掉所有进程级缓存，避免上一个用例的状态渗进下一个用例。"""
    get_settings.cache_clear()

    # 凭据签发器也是懒加载单例，且缓存了 TTL，必须一起重置
    from backend.services import auth_service

    auth_service._issuer = None  # noqa: SLF001 - 测试里刻意重置模块级单例

    # 运行时配置有进程内快照（TTL 5 秒）与 `.env` 快照缓存。不清的话，
    # 「上一个用例往 system_config 写了覆盖项」会带进下一个用例——
    # 而 `DB_PATH` 每次都换，缓存里那份却指向旧库，症状是「查到的配置对不上」。
    from backend.core import crypto, runtime_config, secrets

    runtime_config.invalidate()
    secrets.reset_cache()
    crypto.reset_cache()

    # Engine 按库路径缓存，每个用例都会换库：必须先把上一份的连接全部关掉。
    # 不关的话 Windows 上会一直握着临时库的文件句柄（pytest 清理 tmp_path 时失败），
    # 而且下一个用例可能借到**旧库**的连接——那正是 G-DB3 要防的那种串味。
    from backend.core.engine import reset_engine

    reset_engine()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """把数据库指向用例专属的库（每用例从模板库派生一个，结束删掉）。"""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("INVITE_CODE", "test-invite")
    monkeypatch.setenv("INIT_ADMIN_USER", "")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "")
    # 日志目录也要隔离：v4 起 lifespan 会把日志写进轮转文件，若不指到临时目录，
    # 每跑一次用例就会往仓库的 backend/logs/ 里塞一份，且用例之间互相覆盖。
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))

    # 每个用例一把独立主密钥：密钥加密用例才有东西可解，且互不串味。
    from cryptography.fernet import Fernet

    master_key = tmp_path / "secrets" / "master.key"
    master_key.parent.mkdir(parents=True, exist_ok=True)
    master_key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("PRDWRIGHT_MASTER_KEY_PATH", str(master_key))

    _reset_runtime_state()

    # 建库：每用例一个**新建的库**，从本 worker 的模板库派生（模板只有结构、没有数据）。
    #
    # 为什么不每个用例都跑一次 `alembic upgrade head`：全量 1000+ 用例 × 几百毫秒
    # 就是几分钟，而每个用例拿到的库本来就一模一样。派生之后 `client` 夹具的
    # lifespan 仍会调 `prepare_schema`，走的是「已接管 → upgrade head 空转」这条
    # 分支——正好是真实部署里最常见的那条。
    #
    # 没有 Postgres 时 `template_database()` 抛 `PostgresUnavailable`（带操作指引）：
    # 用例**失败**而不是跳过——静默绿比红更难发现。
    #
    # ⚠️ 维护库地址在这里**先抄一份**：用例可以把 `PRDWRIGHT_TEST_POSTGRES_URL` 改成
    # 一个错的地址（「连不上库时该报什么」本身就是被测行为），而收尾删库必须沿用
    # 建立时那个地址，否则会连删库都删不掉、还留下一个垃圾库。
    maintenance_url = dbsupport.maintenance_url()
    case_database = dbsupport.database_name()
    created = {"done": False}

    def _ensure_case_database() -> None:
        """**按需**建库：从本 worker 的模板库派生一份。

        为什么要懒建：`CREATE DATABASE` 在 Postgres 上是几十毫秒级的真实开销
        （Windows 上约 45~65ms，服务端还要为模板库刷一次检查点），而全量一千多
        个用例里有一批根本不碰数据库（纯函数、提示词、技能包、前端守卫）。
        对它们来说「每用例建一个库」是纯浪费——懒建让它们一分钱不付。
        """
        if created["done"]:
            return
        dbsupport.create_database(case_database, template=dbsupport.template_database())
        created["done"] = True

    # 懒建的挂点：应用只通过 `get_engine()` 建引擎（=> `core.engine.build_engine`），
    # 所以在这一层拦一刀就等于「第一次真的要连库时才建库」。
    from backend.core import engine as engine_module

    original_build_engine = engine_module.build_engine

    def _build_engine(*args, **kwargs):  # noqa: ANN002, ANN003 - 原样透传
        _ensure_case_database()
        return original_build_engine(*args, **kwargs)

    monkeypatch.setattr(engine_module, "build_engine", _build_engine)

    monkeypatch.setenv("DATABASE_URL", dbsupport.database_url(case_database))
    get_settings.cache_clear()

    try:
        yield
    finally:
        # 收尾守卫（比任何单条用例的覆盖面都大）：**每个用例都在守「没有连接被泄漏」**。
        # 池子被占住不会立刻报错，只会让它慢慢变小，最后表现为「接口全部超时」。
        # 用 `engine_stats()` 而不是 `get_engine()`：后者会顺手建一个 Engine，
        # 让从未碰过数据库的用例也开一池连接，并把这条断言变成恒真。
        #
        # ⚠️ 只断 `in_use == 0`。v7 之前这里还有一条 `idle == size`，那是个**假守卫**：
        # 当时 `idle` 是用 `size - in_use` 算的，而 `size` 是 `maxsize`（配置容量），
        # 于是 `in_use == 0` 时 `idle == size` 恒成立——它什么都没守。现在 `idle` 取的是
        # `checkedin()`（真的空闲条数），懒建的池本来就不满，`idle == size` 反而会误报。
        from backend.core.engine import engine_stats

        stats = engine_stats()
        if stats["opened"]:
            assert stats["in_use"] == 0, f"有用例借了连接没归还：{stats}"

        # 先收掉全部连接再删库（池里还连着的话 `DROP DATABASE` 会被挡住）。
        _reset_runtime_state()
        if created["done"]:
            dbsupport.drop_database(case_database, url=maintenance_url)


@pytest.fixture
def client(isolated_db):  # noqa: ARG001 - 依赖 autouse 夹具的执行顺序
    """带生命周期的测试客户端（lifespan 会建表并播种初始数据）。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_off_client(isolated_db, monkeypatch):  # noqa: ARG001
    """`AUTH_ENABLED=false` 下的测试客户端，用于验证开发模式全放行。"""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


def register(client, username="alice", password="password123", invite_code="test-invite"):
    """辅助函数：注册一个用户。"""
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "invite_code": invite_code},
    )


def login(client, username="alice", password="password123"):
    """辅助函数：登录。"""
    return client.post("/api/auth/login", json={"username": username, "password": password})


def assert_requires_login(client, path, method="get", **kwargs):
    """断言「未登录打这个端点」得到 **401 而不是 403**。

    为什么两条都要分清楚、为什么值得抽公共：401 会让前端弹登录窗、403 会让它停在
    「无权限」页——同样一个没登录的用户，看到哪一页完全取决于守卫挂在哪一层。
    这个判断在后台的每个端点上都重复出现，各写一份就迟早有一份写反。
    """
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401, f"{method.upper()} {path} 未登录应当 401：{response.text}"
    assert response.json()["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 直接操作 generation_jobs 仓储的测试辅助（v6 / T16.8）
#
# 仓储现在是「绑定 Session 的实例方法」。用例里有 70+ 处「建一条 / 读一条 / 收尾」的
# 直接断言，是按旧的 `@staticmethod` + dict 形态写的。这里用一个薄适配器收敛：
# 每次调用开一个 `session_scope()`，**底层仍走真实仓储实现**——读回 dict、创建返回 id，
# 于是断言的形状不变。生产代码走的是实例 API（`job_service` 里
# `GenerationJobRepository(session)`），由 chat / generation 的端到端用例覆盖。
# ---------------------------------------------------------------------------


def _job_repo():
    from backend.core.engine import session_scope
    from backend.repositories.generation_job_repo import GenerationJobRepository

    return session_scope, GenerationJobRepository


def job_create(*args, **kwargs):
    session_scope, repo = _job_repo()
    with session_scope() as session:
        job = repo(session).create(*args, **kwargs)
        return int(job.id or 0)


def job_get(owner_id, job_id):
    session_scope, repo = _job_repo()
    with session_scope() as session:
        job = repo(session).get(owner_id, job_id)
        return job.model_dump() if job else None


def job_find_active(owner_id, **kwargs):
    session_scope, repo = _job_repo()
    with session_scope() as session:
        job = repo(session).find_active(owner_id, **kwargs)
        return job.model_dump() if job else None


def job_list_by_owner(owner_id, **kwargs):
    session_scope, repo = _job_repo()
    with session_scope() as session:
        return [job.model_dump() for job in repo(session).list_by_owner(owner_id, **kwargs)]


def job_finish(*args, **kwargs):
    """收尾成 completed，返回**改到的行数**（#79：条件写，判据是影响行数）。"""
    session_scope, repo = _job_repo()
    with session_scope() as session:
        return repo(session).finish(*args, **kwargs)


def job_fail(*args, **kwargs):
    """收尾成 failed，返回**改到的行数**（#79：条件写，判据是影响行数）。"""
    session_scope, repo = _job_repo()
    with session_scope() as session:
        return repo(session).fail(*args, **kwargs)


def job_persist_progress(*args, **kwargs):
    session_scope, repo = _job_repo()
    with session_scope() as session:
        repo(session).persist_progress(*args, **kwargs)


def job_cancel_running(*args, **kwargs):
    """按方案取消（测试薄封装）：错误码 / 文案取状态机的取消口径（#79）。"""
    session_scope, repo = _job_repo()
    kwargs.setdefault("error_code", "CANCELLED")
    kwargs.setdefault("error_message", "方案已删除，任务已取消")
    with session_scope() as session:
        return repo(session).cancel_running(*args, **kwargs)


# ---------------------------------------------------------------------------
# 直接操作认证 / 配置 / 后台仓储的测试辅助（v6 / T16.9 + T16.10）
#
# 仓储现在是「绑定 Session 的实例方法」，而用例是按旧的 `@staticmethod` + dict 形态
# 写的（约 120 处）。这里给每个类一个**薄适配器实例**：每次调用开一个 `session_scope()`，
# 底层仍走真实仓储实现，读回 dict、写原样透传，于是断言一个字都不用改。
#
# 用法上只换导入行：`from backend.repositories.auth_repo import UserRepository`
# → `from conftest import users as UserRepository`。这样「迁移仓储」不会变成
# 「重写一百多个断言」——后者会让 review 无法分辨「是仓储改坏了还是断言改坏了」。
#
# 生产代码不走这里：服务层直接 `UserRepository(session)`，由端到端用例覆盖。
# ---------------------------------------------------------------------------


def _scoped(repo_cls):
    """返回一个在 `session_scope()` 里调用 `repo_cls` 的上下文管理器。"""
    from backend.core.engine import session_scope

    return session_scope(), repo_cls


def _dump(row):
    return row.model_dump() if row is not None else None


def _dump_all(rows):
    return [row.model_dump() for row in rows]


class _UserRepoAdapter:
    """`UserRepository` 的用例侧适配器（读写都开短事务）。"""

    def create(self, *args, **kwargs):
        scope, repo = _scoped(_user_repo())
        with scope as session:
            user = repo(session).create(*args, **kwargs)
            return int(user.id or 0)

    def get_by_username(self, *args, **kwargs):
        scope, repo = _scoped(_user_repo())
        with scope as session:
            return _dump(repo(session).get_by_username(*args, **kwargs))

    def get_by_id(self, *args, **kwargs):
        scope, repo = _scoped(_user_repo())
        with scope as session:
            return _dump(repo(session).get_by_id(*args, **kwargs))

    def list_all(self):
        scope, repo = _scoped(_user_repo())
        with scope as session:
            return [row.as_dict() for row in repo(session).list_all()]

    def count(self):
        scope, repo = _scoped(_user_repo())
        with scope as session:
            return repo(session).count()

    def count_admins(self):
        scope, repo = _scoped(_user_repo())
        with scope as session:
            return repo(session).count_admins()

    def set_is_admin(self, *args, **kwargs):
        scope, repo = _scoped(_user_repo())
        with scope as session:
            repo(session).set_is_admin(*args, **kwargs)

    def set_active(self, *args, **kwargs):
        scope, repo = _scoped(_user_repo())
        with scope as session:
            repo(session).set_active(*args, **kwargs)

    def update_password(self, *args, **kwargs):
        scope, repo = _scoped(_user_repo())
        with scope as session:
            repo(session).update_password(*args, **kwargs)

    def delete(self, *args, **kwargs):
        scope, repo = _scoped(_user_repo())
        with scope as session:
            repo(session).delete(*args, **kwargs)


class _AuthSessionRepoAdapter:
    """`AuthSessionRepository` 的用例侧适配器。"""

    def create(self, *args, **kwargs):
        scope, repo = _scoped(_auth_session_repo())
        with scope as session:
            repo(session).create(*args, **kwargs)

    def replace_for_user(self, *args, **kwargs):
        scope, repo = _scoped(_auth_session_repo())
        with scope as session:
            repo(session).replace_for_user(*args, **kwargs)

    def fetch_valid(self, *args, **kwargs):
        scope, repo = _scoped(_auth_session_repo())
        with scope as session:
            return _dump(repo(session).fetch_valid(*args, **kwargs))

    def list_by_user(self, *args, **kwargs):
        scope, repo = _scoped(_auth_session_repo())
        with scope as session:
            return _dump_all(repo(session).list_by_user(*args, **kwargs))

    def delete(self, *args, **kwargs):
        scope, repo = _scoped(_auth_session_repo())
        with scope as session:
            repo(session).delete(*args, **kwargs)

    def delete_expired(self):
        scope, repo = _scoped(_auth_session_repo())
        with scope as session:
            return repo(session).delete_expired()

    def delete_by_user(self, *args, **kwargs):
        scope, repo = _scoped(_auth_session_repo())
        with scope as session:
            return repo(session).delete_by_user(*args, **kwargs)


class _LoginFailureRepoAdapter:
    """`LoginFailureRepository` 的用例侧适配器。"""

    def record(self, *args, **kwargs):
        scope, repo = _scoped(_login_failure_repo())
        with scope as session:
            repo(session).record(*args, **kwargs)

    def count_since(self, *args, **kwargs):
        scope, repo = _scoped(_login_failure_repo())
        with scope as session:
            return repo(session).count_since(*args, **kwargs)

    def clear(self, *args, **kwargs):
        scope, repo = _scoped(_login_failure_repo())
        with scope as session:
            return repo(session).clear(*args, **kwargs)

    def purge_before(self, *args, **kwargs):
        scope, repo = _scoped(_login_failure_repo())
        with scope as session:
            return repo(session).purge_before(*args, **kwargs)


class _InviteCodeRepoAdapter:
    """`InviteCodeRepository` 的用例侧适配器。"""

    def create(self, *args, **kwargs):
        scope, repo = _scoped(_invite_code_repo())
        with scope as session:
            repo(session).create(*args, **kwargs)

    def get(self, *args, **kwargs):
        scope, repo = _scoped(_invite_code_repo())
        with scope as session:
            return _dump(repo(session).get(*args, **kwargs))

    def consume(self, *args, **kwargs):
        scope, repo = _scoped(_invite_code_repo())
        with scope as session:
            return repo(session).consume(*args, **kwargs)

    def count(self):
        scope, repo = _scoped(_invite_code_repo())
        with scope as session:
            return repo(session).count()

    def list_all(self):
        scope, repo = _scoped(_invite_code_repo())
        with scope as session:
            return _dump_all(repo(session).list_all())

    def delete(self, *args, **kwargs):
        scope, repo = _scoped(_invite_code_repo())
        with scope as session:
            return repo(session).delete(*args, **kwargs)


class _SystemConfigRepoAdapter:
    """`SystemConfigRepository` 的用例侧适配器。"""

    def get_all(self):
        scope, repo = _scoped(_system_config_repo())
        with scope as session:
            return repo(session).get_all()

    def upsert_many(self, *args, **kwargs):
        scope, repo = _scoped(_system_config_repo())
        with scope as session:
            repo(session).upsert_many(*args, **kwargs)

    def delete_many(self, *args, **kwargs):
        scope, repo = _scoped(_system_config_repo())
        with scope as session:
            return repo(session).delete_many(*args, **kwargs)


class _ModelProviderRepoAdapter:
    """`ModelProviderRepository` 的用例侧适配器。"""

    def list_all(self):
        scope, repo = _scoped(_model_provider_repo())
        with scope as session:
            return [_dump(row) for row in repo(session).list_all()]

    def get(self, *args, **kwargs):
        scope, repo = _scoped(_model_provider_repo())
        with scope as session:
            return _dump(repo(session).get(*args, **kwargs))

    def create(self, *args, **kwargs):
        scope, repo = _scoped(_model_provider_repo())
        with scope as session:
            repo(session).create(*args, **kwargs)

    def update(self, *args, **kwargs):
        scope, repo = _scoped(_model_provider_repo())
        with scope as session:
            return repo(session).update(*args, **kwargs)

    def delete(self, *args, **kwargs):
        scope, repo = _scoped(_model_provider_repo())
        with scope as session:
            return repo(session).delete(*args, **kwargs)

    def count(self):
        scope, repo = _scoped(_model_provider_repo())
        with scope as session:
            return repo(session).count()


class _AdminStatsRepoAdapter:
    """`AdminStatsRepository` 的用例侧适配器（只读）。"""

    def user_usage(self):
        scope, repo = _scoped(_admin_stats_repo())
        with scope as session:
            return [row.as_dict() for row in repo(session).user_usage()]

    def platform_usage(self):
        scope, repo = _scoped(_admin_stats_repo())
        with scope as session:
            return repo(session).platform_usage().as_dict()

    def daily(self, *args, **kwargs):
        scope, repo = _scoped(_admin_stats_repo())
        with scope as session:
            return [row.as_dict() for row in repo(session).daily(*args, **kwargs)]

    def top_users(self, *args, **kwargs):
        scope, repo = _scoped(_admin_stats_repo())
        with scope as session:
            return [row.as_dict() for row in repo(session).top_users(*args, **kwargs)]


class _AuditRepoAdapter:
    """`AuditRepository` 的用例侧适配器。"""

    def record(self, *args, **kwargs):
        scope, repo = _scoped(_audit_repo())
        with scope as session:
            repo(session).record(*args, **kwargs)

    def list_recent(self, *args, **kwargs):
        scope, repo = _scoped(_audit_repo())
        with scope as session:
            return _dump_all(repo(session).list_recent(*args, **kwargs))


def _user_repo():
    from backend.repositories.auth_repo import UserRepository
    return UserRepository


def _auth_session_repo():
    from backend.repositories.auth_repo import AuthSessionRepository
    return AuthSessionRepository


def _login_failure_repo():
    from backend.repositories.auth_repo import LoginFailureRepository
    return LoginFailureRepository


def _invite_code_repo():
    from backend.repositories.auth_repo import InviteCodeRepository
    return InviteCodeRepository


def _system_config_repo():
    from backend.repositories.runtime_config_repo import SystemConfigRepository
    return SystemConfigRepository


def _model_provider_repo():
    from backend.repositories.runtime_config_repo import ModelProviderRepository
    return ModelProviderRepository


def _admin_stats_repo():
    from backend.repositories.admin_repo import AdminStatsRepository
    return AdminStatsRepository


def _audit_repo():
    from backend.repositories.admin_repo import AuditRepository
    return AuditRepository


users = _UserRepoAdapter()
auth_sessions = _AuthSessionRepoAdapter()
login_failures = _LoginFailureRepoAdapter()
invite_codes = _InviteCodeRepoAdapter()
system_config = _SystemConfigRepoAdapter()
model_providers = _ModelProviderRepoAdapter()
admin_stats = _AdminStatsRepoAdapter()
audit_log = _AuditRepoAdapter()


# ---------------------------------------------------------------------------
# 技能包：注册表与 PRD 技能层的用例都要在临时目录里「造一个包」
# ---------------------------------------------------------------------------

# PRD 技能包的资源清单（语义键 -> 包内相对路径），与 `skills/prd-generator/skill.yaml` 对齐
PRD_SKILL_ARTIFACTS = {
    "field_schema": "references/field-schema.json",
    "prd_template": "references/prd-template.md",
    "writing_rules": "references/writing-rules.md",
}


@pytest.fixture
def temp_skills_root(tmp_path, monkeypatch):
    """把技能根目录指到用例专属的临时目录，并在结束后清掉读盘缓存。

    全部技能相关的用例都走它：**不碰仓库里真实的 `skills/`**，这样用例既不受本地
    技能包内容变化影响，也不会在跑测试时读到别的用例留下的缓存。
    """
    from backend.services import skill_registry

    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("PRDWRIGHT_SKILLS_DIR", str(root))
    skill_registry.clear_caches()
    yield root
    skill_registry.clear_caches()


def write_skill_package(
    skills_root,
    name: str,
    *,
    applies_to: tuple[str, ...] = ("prd",),
    version: str = "0.1.0",
    artifacts: dict[str, str] | None = None,
):
    """在临时 skills 根目录下造一个技能包，返回它的目录。

    它同时也是那份 `skill.yaml` 格式的可执行说明：**放这里而不是各用例文件里，
    是为了让「一个技能包长什么样」只有一处定义**。

    Args:
        skills_root: skills 根目录（即 `temp_skills_root` 夹具给出的路径）。
        name: 技能包目录名，同时也是 `name` 字段。
        applies_to: 声明的适用产物；传空元组则不写这个字段。
        version: 版本号。
        artifacts: `语义键 -> 包内相对路径`；缺省用 PRD 那三份。

    Returns:
        技能包目录的路径。
    """
    skill_dir = skills_root / name
    chosen = dict(PRD_SKILL_ARTIFACTS if artifacts is None else artifacts)
    for relative_path in chosen.values():
        target = skill_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"初始内容：{relative_path}\n", encoding="utf-8")

    lines = [f"name: {name}", f"version: {version}"]
    if applies_to:
        lines.append("applies_to:")
        lines.extend(f"  - {kind}" for kind in applies_to)
    lines.append("references:")
    for key, relative_path in chosen.items():
        lines.append(f"  - key: {key}")
        lines.append(f"    path: {relative_path}")
    (skill_dir / "skill.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return skill_dir


def write_longer(path, suffix: str) -> None:
    """改写文件并保证「修改时间 + 大小」两项都变化。

    单纯往同一长度改写内容时，字节数不变、某些文件系统上的时间戳精度也不足，
    那样会让「改了要立刻生效」这类用例验不到真正的重载路径。
    """
    path.write_text(
        f"{path.read_text(encoding='utf-8')}新增行：{suffix}\n" * 2, encoding="utf-8"
    )
