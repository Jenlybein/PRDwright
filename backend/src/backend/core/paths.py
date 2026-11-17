"""旧 SQLite 库文件路径解析（v9 起不再用于连接）。

为什么还留着：单后端之后本项目不再读 SQLite 文件，但「启动被拒绝时点名看见的是哪个旧
文件」需要一处和 `Settings` 同口径的路径解析。它放在最下游，谁都可以往上依赖，不成环。
"""

from __future__ import annotations

from pathlib import Path

from backend.core.config import get_settings


def default_db_file() -> Path:
    """旧 SQLite 库文件路径（`DB_PATH` > `backend/prdwright.db`）。

    ⚠️ **不用于连接**：v9 起只支持 Postgres，这个函数只服务于「拒绝启动时点名旧文件」。
    每次都读 `get_settings()`，不缓存（测试会换路径）；父目录不创建——对一个即将被拒绝
    的旧路径做副作用没有意义。
    """
    path = get_settings().db_file
    return path
