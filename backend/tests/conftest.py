"""pytest 公共夹具。

认证相关的测试有一个硬性前提：**每个用例都要用独立的数据库**。
否则用例之间会互相看见对方注册的用户、核销掉的邀请码，测试结果随执行顺序漂移。

配置是 `lru_cache` 单例，所以这里不是「改环境变量」就完事——
必须同时 `cache_clear()`，让下一次 `get_settings()` 重新读取。

## 建库：一次建模板，每用例拷一份

每个用例都跑一次完整的 `alembic upgrade head` 会白花十几秒（975 个用例 × 约 12ms）。
所以这里改成**每个进程建一次模板库**（还是走 `prepare_schema`，与真实启动同一条路），
之后每个用例只做一次文件拷贝——拷贝的是「已经接管好的库」，`client` 夹具的 lifespan
再调 `prepare_schema` 时走的是 `upgrade head` 空转，正好也是真实部署里最常见的那条分支。
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile

import pytest

from backend.core.config import get_settings

# 进程级模板库（每个 xdist worker 建一次）。
_TEMPLATE_DB: pathlib.Path | None = None


def _schema_template() -> pathlib.Path:
    """建一次「最新结构 + 已登记 alembic 版本」的模板库，之后一直复用。"""
    global _TEMPLATE_DB
    if _TEMPLATE_DB is not None and _TEMPLATE_DB.exists():
        return _TEMPLATE_DB

    directory = pathlib.Path(tempfile.mkdtemp(prefix="prdwright-schema-"))
    path = directory / "template.db"

    # ⚠️ 用独立的 Engine，不碰 `get_engine()`：后者绑的是当前 `DB_PATH`，
    # 而模板要在任何用例的库路径都还不存在时就建好。
    from backend.core.engine import build_engine
    from backend.core.schema import prepare_schema

    engine = build_engine(path)
    try:
        prepare_schema(engine)
    finally:
        # dispose 会把 WAL 落盘并关掉句柄，否则拷贝出来的可能缺最近几页
        engine.dispose()

    _TEMPLATE_DB = path
    return path


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
    """把数据库指向用例专属的临时文件，并在结束后还原运行时状态。"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
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

    # 建库：从模板拷一份（模板是 `prepare_schema` 建的，与真实启动同一条路）。
    #
    # 为什么不每个用例都跑一次 `alembic upgrade head`：975 个用例 × 约 12ms
    # 就是十几秒，而每个用例拿到的库本来就一模一样。拷贝之后 `client` 夹具的
    # lifespan 仍会调 `prepare_schema`，走的是「已接管 → upgrade head 空转」这条
    # 分支——正好是真实部署里最常见的那条。
    shutil.copyfile(_schema_template(), db_path)

    yield

    # 收尾守卫（比任何单条用例的覆盖面都大）：**每个用例都在守「没有连接被泄漏」**。
    # 池子被占住不会立刻报错，只会让它慢慢变小，最后表现为「接口全部超时」。
    # 用 `engine_stats()` 而不是 `get_engine()`：后者会顺手建一个 Engine，
    # 让从未碰过数据库的用例也开一池连接，并把这条断言变成恒真。
    from backend.core.engine import engine_stats

    stats = engine_stats()
    if stats["opened"]:
        assert stats["in_use"] == 0, f"有用例借了连接没归还：{stats}"
        assert stats["idle"] == stats["size"], f"连接池少了连接（不是没还就是补建失败）：{stats}"

    _reset_runtime_state()


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
    session_scope, repo = _job_repo()
    with session_scope() as session:
        repo(session).finish(*args, **kwargs)


def job_fail(*args, **kwargs):
    session_scope, repo = _job_repo()
    with session_scope() as session:
        repo(session).fail(*args, **kwargs)


def job_persist_progress(*args, **kwargs):
    session_scope, repo = _job_repo()
    with session_scope() as session:
        repo(session).persist_progress(*args, **kwargs)


def job_cancel_running(*args, **kwargs):
    session_scope, repo = _job_repo()
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


def _provider_dict(row):
    """`model_providers` 的行 → 字典。

    去掉 `builtin`：它是遗留列，旧的显式列清单（`_PROVIDER_COLUMNS`）从不返回它，
    用例据此断言过「返回形状里没有这一列」。模型现在有这一列，形状要在这里对齐。
    """
    if row is None:
        return None
    data = row.model_dump()
    data.pop("builtin", None)
    return data


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
            return [_provider_dict(row) for row in repo(session).list_all()]

    def get(self, *args, **kwargs):
        scope, repo = _scoped(_model_provider_repo())
        with scope as session:
            return _provider_dict(repo(session).get(*args, **kwargs))

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
