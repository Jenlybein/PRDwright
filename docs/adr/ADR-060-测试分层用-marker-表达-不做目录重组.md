# ADR-060：测试分层用 marker 表达，不做目录重组

> 源：v5 层（原 `docs/architecture/v5/ADR.md`，已并入本目录）｜ [索引](./README.md)

> 状态：已评审，待落地（T09）

### 背景

54 个测试文件平铺在 `backend/tests/`。QA 诊断给出的分层现状是
「unit / service 334 例（38.7%）、接口层 529 例（61.3%）」，但那 61.3% 是虚高的：
`test_generation_job.py`、`test_clarify.py`、`test_auto_clarify.py` 等核心文件
主要直接调 `job_service.create_job` / `dispatch` / `open_stream`，
并大量以 `# noqa: SLF001` 直触 `_RUNTIMES` / `_TASKS` / `_finalize` 等私有成员，
`client` 只用于造数据。真正做 HTTP 契约断言的集中在 admin 组、auth、documents、
solutions、export。

直觉解法是建 `unit/` `service/` `api/` 三个子目录，把 54 个文件搬进去。

### 决策

不搬目录。 54 个文件原地不动，每个文件顶部加一行 `pytestmark = pytest.mark.<layer>`，
四个 marker（`unit` / `service` / `api` / `integration`）在 `pyproject.toml` 注册。

配套一条静态守卫，断言「没有 marker 的测试文件数为 0」。

### 为什么

- 搬目录只换容器不换内容，收益为零；代价是 54 个文件的 git 历史被打散
  （`git log --follow` 失效），且下次想再调整又是一次大 churn。
- marker 能做到目录做不到的两件事：`pytest -m api` 只跑 HTTP 契约、
  `pytest -m "not integration"` 快速循环。
- 可行性有既有依据：本仓库已有 `backend/tests/test_layering.py`，
  用静态扫描守「services / repositories 不得反向 import api」，
  并且它配了两条 `..._is_not_vacuous` 自检用例。这是本项目既有的 idiom。

### 代价

分层因此是约定而非物理约束：有人不加 marker 也能混过去。
缓解手段就是那条守卫——它同时还能防「新建测试文件时忘标」。

### 落地清单

- `backend/pyproject.toml` 注册四个 marker。
- 54 个 `backend/tests/test_*.py` 各加 1 行 `pytestmark`。
- 守卫 1 条（含自检用例）。

---
