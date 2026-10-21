"""按键名取密钥值：先看进程环境，再看 `.env` 文件。

为什么需要这一层
----------------
`pydantic-settings` 读 `.env` 时**不会把内容写进 `os.environ`**——它只在构造
`Settings` 时把值填进字段。于是出现一个很隐蔽的落差：

    # .env 里明明写着 DEEPSEEK_API_KEY=sk-xxx
    os.environ.get("DEEPSEEK_API_KEY")   # → None
    get_settings().ark_api_key           # → sk-xxx（只有**已声明**的字段能读到）

内置那三家之所以没暴露这个问题，是因为它们都是 `Settings` 的声明字段、走
`require_api_key()` 取。而**后台新增的供应商**是运行时数据，`Settings` 里没有
对应字段，只能按键名去取——这时 `os.environ` 就是空的，界面会一直报
「尚未配置 XXX_API_KEY」，而用户明明已经写在 `.env` 里了。

所以这里补一条「按键名查找」的路：`os.environ` 优先（容器/CI 里通常用它注入），
其次按 mtime 缓存的 `.env` 快照。

顺带的好处
----------
改 `.env` 里的密钥**不必重启**：mtime 一变就重读。这与 T3 的目标一致
（配置改了下次生成就生效），而对密钥来说是纯收益——密钥本来就不该进数据库。
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.core.config import ENV_FILE

# `.env` 快照：路径 -> (mtime, 键值表)。mtime 变了才重读，
# 避免每次构造 LLM 都去磁盘读一遍文件（那发生在生成主链路上）。
_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 `.env` 解析：`KEY=VALUE`，忽略注释与空行，去掉成对引号。

    刻意不用 `dotenv_values`：它对 `export` 前缀、多行值、变量插值都有一套规则，
    而这里只需要「读出一个密钥」这一件事。规则越少，越不会出现
    「某个写法在 pydantic 里生效、在这里不生效」的分叉。
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _env_file_path() -> Path:
    """`.env` 的位置。

    做成函数而不是直接引用常量，是为了**测试能换掉它**：默认指向项目真实的
    `.env`，而用例需要「一个没有密钥的世界」来验证缺密钥的提示文案。
    直接读常量的话，本机真实配置过 ARK_API_KEY 就会让那条用例永远绿——
    看着像通过，其实什么都没验。
    """
    return Path(ENV_FILE)


def env_file_values() -> dict[str, str]:
    """当前 `.env` 的键值表（按 mtime 缓存）。读不到文件时给空表。"""
    path = _env_file_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}

    key = str(path)
    cached = _cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    values = _parse_env_file(path)
    _cache[key] = (mtime, values)
    return values


def lookup_env(name: str) -> str:
    """按变量名取密钥值；取不到返回空串（**不抛异常**）。

    顺序：进程环境 > `.env` 文件。

    ⚠️ 判据是「**键是否存在**」而不是「值是否非空」：进程环境里显式设了
    `ARK_API_KEY=""` 时，语义是「这个键被清空了」，必须就地返回空串，
    不能再回落到 `.env` 里那份。否则「用空值临时禁用一个供应商」这个
    运维手段会失效——而它看起来完全正常，只是禁用不掉。
    这也是 `pydantic-settings` 的口径：环境变量优先，空串也算值。
    """
    if name and name in os.environ:
        return os.environ[name]
    if not name:
        return ""
    return env_file_values().get(name, "")


def is_present(name: str) -> bool:
    """这个变量是否有非空值。给后台显示 `key_present` 用——只回布尔，不回值。"""
    return bool(lookup_env(name))


def reset_cache() -> None:
    """清掉 `.env` 快照。测试改完文件后调用；生产代码不需要（mtime 会自己变）。"""
    _cache.clear()
