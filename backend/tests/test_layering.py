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

# 具体供应商 SDK。业务代码只认 `llm_factory.get_llm()` 与统一的 `StreamingChatModel`
# 协议，绝不直接 import 任何一家的 SDK。
#
# 六个协议各自的 SDK 都要扫到：少扫一个，那一家就可能在任意 service 里被直连，
# 而「换供应商只改工厂」的承诺就破了。
_VENDOR_SDK_RE = re.compile(
    r"^\s*(?:from|import)\s+"
    r"(arkruntime|langchain_openai|langchain_anthropic|"
    r"langchain_google_genai|langchain_nvidia_ai_endpoints|langchain_huggingface)\b",
    re.MULTILINE,
)

# 唯一允许直连供应商 SDK 的文件。
#
# ⚠️ 这里从两个文件（`llm_factory.py` + `ark_llm.py`）收敛成一个：方舟适配器已并入
# `llm_factory.py`。白名单每多一个文件，就多一处「新加适配器时忘了登记」的机会；
# 收敛之后规则只剩一句：**只有工厂能碰供应商 SDK**。
_VENDOR_SDK_ALLOWED = frozenset({"llm_factory.py"})


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


def test_vendor_sdk_detector_is_not_vacuous():
    """自检：供应商 SDK 扫描规则必须真能命中直连写法。"""
    assert _VENDOR_SDK_RE.search("from arkruntime import AsyncArk")
    assert _VENDOR_SDK_RE.search("import langchain_openai")
    assert _VENDOR_SDK_RE.search("from langchain_anthropic import ChatAnthropic")
    # 新加的三个协议包也要真的被扫到（少扫一个，那家就能被直连）
    assert _VENDOR_SDK_RE.search("from langchain_google_genai import ChatGoogleGenerativeAI")
    assert _VENDOR_SDK_RE.search(
        "from langchain_nvidia_ai_endpoints import ChatNVIDIA"
    )
    assert _VENDOR_SDK_RE.search("from langchain_huggingface import ChatHuggingFace")
    # 抽象层与自家的工厂不该被误报
    assert not _VENDOR_SDK_RE.search("from langchain_core.messages import AIMessageChunk")
    assert not _VENDOR_SDK_RE.search(
        "from backend.services.llm_factory import VolcanoArkChatModel"
    )


def test_only_adapters_import_vendor_sdk():
    """业务层不得直接 import 供应商 SDK（方舟 / OpenAI / Anthropic）。

    换供应商时要改的只有工厂（`llm_factory`，六家适配器都在里面）。
    若某个 service 直接 import `arkruntime` / `langchain_openai`，换供应商就成了
    全仓大改；用量映射这类适配逻辑也会被复制到各处，迟早走形。
    """
    offenders: list[str] = []
    for path in sorted((_BACKEND_SRC / "services").glob("*.py")):
        if path.name in _VENDOR_SDK_ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _VENDOR_SDK_RE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.name}:{line_no}")

    assert offenders == [], (
        "业务层出现供应商 SDK 直连（应经 `llm_factory.get_llm()`）：" + "；".join(offenders)
    )
