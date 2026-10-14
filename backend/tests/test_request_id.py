"""`request_id` 中间件（`api/deps.py::RequestIdMiddleware`）与请求上下文的契约测试。

`request_id` 的价值是「用户能把它念给维护者」。要兑现这一点，三件事缺一不可：

1. **回写响应头**——包括被认证中间件拦下的 401，所以本中间件注册在最外层；
2. **可被服务层读到**——通过 `ContextVar`（`core/request_context.py`），
   这是中间件与深层服务之间唯一的通道，`run_summary` 的追踪号就靠它；
3. **不采信外部原样输入**——只接受合规的值，否则等于把响应头的控制权交给调用方。

同时它必须是纯 ASGI：本项目有 SSE 长连接，`BaseHTTPMiddleware` 会重包响应体。
"""

from __future__ import annotations

from backend.core.request_context import current_request_id


def test_response_carries_a_generated_request_id(client):
    """没带请求头时也要生成一个并回写，任何时候响应里都得有号可念。"""
    response = client.get("/health")

    value = response.headers.get("X-Request-ID", "")
    assert value
    assert len(value) <= 64


def test_client_supplied_id_is_echoed_when_valid(client):
    """合规的入站 id 原样回写（含 64 字符边界），便于跨服务串联。"""
    response = client.get("/health", headers={"X-Request-ID": "trace-abc_123"})
    assert response.headers["X-Request-ID"] == "trace-abc_123"

    boundary = "a" * 64
    response = client.get("/health", headers={"X-Request-ID": boundary})
    assert response.headers["X-Request-ID"] == boundary


def test_invalid_request_id_is_replaced_not_echoed(client):
    """超长或含非法字符的 id 一律丢弃并另生成，绝不原样透传。"""
    for bad in ("a" * 65, "bad;id", "with space"):
        response = client.get("/health", headers={"X-Request-ID": bad})
        assert response.headers["X-Request-ID"] != bad
        assert response.headers["X-Request-ID"]


def test_auth_rejected_401_also_carries_request_id(client):
    """认证中间件拦下的 401 也必须带号——这正是把它放在最外层的原因。"""
    response = client.get("/api/jobs/1")

    assert response.status_code == 401
    assert response.headers.get("X-Request-ID")


def test_context_var_is_visible_to_route_handler():
    """中间件写入的 id 必须能被「服务层」读到——run_summary 记录追踪号的唯一通道。

    用一个探针应用把链路缩到最小：`RequestIdMiddleware` + 一个回读上下文的路由。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.deps import RequestIdMiddleware

    probe = FastAPI()
    probe.add_middleware(RequestIdMiddleware)

    @probe.get("/probe")
    def _probe() -> dict:
        # 路由处理函数里读到的，就是深层服务层会读到的同一个值
        return {"request_id": current_request_id()}

    with TestClient(probe) as probe_client:
        supplied = probe_client.get("/probe", headers={"X-Request-ID": "probe-1"})
        assert supplied.json()["request_id"] == "probe-1"
        assert supplied.headers["X-Request-ID"] == "probe-1"

        generated = probe_client.get("/probe")
        assert generated.json()["request_id"]
        assert generated.json()["request_id"] == generated.headers["X-Request-ID"]
