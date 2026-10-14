"""分层的静态不变量：services / repositories 不得反向依赖 api。

依赖方向约定为 `api → services → repositories → core`。`services/` 与
`repositories/` 若回头 import `api/`，就形成反向依赖（甚至循环 import）：
业务层被接口层的数据结构绑死，改一处响应模型就得动业务代码。

历史背景：`StreamEvent` / `QuestionsConfig` 曾经定义在 `api/schemas.py`，
被 5 个 service 反向 import。本次把这三个契约下沉到 `core/contracts.py`，
`api/schemas.py` 只做 re-export。本文件是**防回退守卫**：跑一次 pytest
就能发现有人又把 `from backend.api` 写了回去。

扫描规则简单到不会被绕过——整份源码文本里出现 `from backend.api` 或
`import backend.api` 即判失败（注释里提到也算）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# 仓库根 → 后端源码目录
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_SRC = _REPO_ROOT / "backend" / "src" / "backend"

# 禁止反向依赖接口层的两个目录
_GUARDED_DIRS = ("services", "repositories")

# 匹配 `from backend.api...` 或 `import backend.api...` 两种写法
_FORBIDDEN_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+backend\.api(?:\.[\w.]+)?\s+import\b|import\s+backend\.api(?:\.[\w.]+)?\b)",
    re.MULTILINE,
)


def _scan_for_backward_imports(directory: Path) -> list[str]:
    """扫描目录下所有 .py 文件，返回「反向 import api」的违规行。

    Args:
        directory: 待扫描的目录（如 `services/`）。

    Returns:
        形如 ``文件相对路径:行号 → 源码行`` 的违规描述列表；无违规则为空列表。
    """
    offenders: list[str] = []
    if not directory.exists():
        return offenders
    for path in sorted(directory.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in _FORBIDDEN_IMPORT_RE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            line = text.splitlines()[line_no - 1].strip()
            offenders.append(f"{path.relative_to(_BACKEND_SRC)}:{line_no} → {line}")
    return offenders


def test_forbidden_import_detector_is_not_vacuous():
    """自检：扫描规则必须真能抓到反向依赖的写法。

    一条「永远不报错」的守卫等于没有守卫，所以先用一段已知的坏代码
    证明正则睁着眼——避免正则写错却静默失明。
    """
    assert _FORBIDDEN_IMPORT_RE.search("from backend.api.schemas import StreamEvent")
    assert _FORBIDDEN_IMPORT_RE.search("from backend.api import schemas")
    assert _FORBIDDEN_IMPORT_RE.search("import backend.api.schemas")
    # 正向依赖（core / services）不该被误报
    assert not _FORBIDDEN_IMPORT_RE.search("from backend.core.contracts import StreamEvent")
    assert not _FORBIDDEN_IMPORT_RE.search("from backend.services import document_service")


@pytest.mark.parametrize("layer", _GUARDED_DIRS)
def test_layer_does_not_import_api(layer):
    """`services/` 与 `repositories/` 不得 import `api/`。

    契约已下沉到 `core/contracts.py`：需要 `StreamEvent` / `QuestionsConfig`
    时从 `core` 取，绝不回头 import 接口层。
    """
    offenders = _scan_for_backward_imports(_BACKEND_SRC / layer)

    assert not offenders, (
        f"`{layer}/` 出现对 `api/` 的反向依赖（契约应放在 core/contracts.py）："
        + "；".join(offenders)
    )
