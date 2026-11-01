# ADR-067：三工程开 coverage，只看不设阈值

> 源：v5 层（原 `docs/architecture/v5/ADR.md`，已并入本目录）｜ [索引](./README.md)

> 状态：已评审，待落地（T10）

### 背景

两个 `vitest.config.ts` 都只配了 `environment: 'jsdom'`，无 coverage；
后端未配 `--cov`。因此 0.752 / 0.776 两个密度数字不说明任何覆盖率。

### 决策

三个工程都能出覆盖率数字。v5 不设任何百分比门槛。

### 为什么

设阈值会立刻诱发「为达标而写的测试」（给 getter 写断言），是典型的指标扭曲。
v5 的目标是看见，不是达标。 门槛留到看到真实数字之后再定。

### 代价

每次跑全量多几秒。

### 落地清单

- `frontend/vitest.config.ts`、`frontend_admin/vitest.config.ts` 加 coverage 配置。
- `backend/pyproject.toml` 配 `--cov`。
- CI 三工程各自产出报告（不强校验）。

---
