"""数据库文件路径解析（唯一实现）。

为什么单独一个模块：`core/db.py`、`core/database.py`、`core/engine.py` 都需要「当前配置下
的库文件路径」，而它们之间不能互相 import（`db → database`、`db → engine` 是既有方向，
反过来会成环）。把这一份路径逻辑放在最下游，谁都可以往上依赖，不会成环。
"""

from __future__ import annotations

from pathlib import Path

from backend.core.config import get_settings


def default_db_file() -> Path:
    """当前配置下的数据库文件路径，并确保其父目录存在。

    ⚠️ 每次都读 `get_settings()`，不缓存：路径可以在运行期变化（测试装置每个用例换一个
    临时库）。缓存了的话，切库之后拿到的还是旧路径——症状是「新用例看见了上一个用例的数据」。
    """
    path = get_settings().db_file
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
