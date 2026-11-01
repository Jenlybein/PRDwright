"""`backend` 包的命名空间根。

**这里刻意是空的**：唯一的 FastAPI 应用住在 `backend.main`（`backend.main:app`）。
包级 `__init__` 里不要再建 `FastAPI()`——它曾有过一个自带 `/health` 的第二个应用，
全仓无人引用，却会让人误以为「应用定义在包的入口」而把路由加进一个永远不会被
uvicorn 加载的对象。启动与体检各自的唯一真源：应用 → `main.py`，`/health` →
`api/health.py`。守卫见 `tests/test_entrypoint.py`。
"""
