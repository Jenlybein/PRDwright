# ADR-068：CI 补齐 admin job

> 源：v5 层（原 `docs/architecture/v5/ADR.md`，已并入本目录）｜ [索引](./README.md)

> 状态：已评审，待落地（T01）

### 背景

`.github/workflows/ci.yml` 只有 `backend` 与 `frontend` 两个 job。
`frontend_admin` 的 12 个 spec / 114 个用例本地全绿，但从未在 CI 上跑过。

### 决策

加 `frontend-admin` job，三步：`npm ci` → `npm run test:unit` → `npm run build`。
**同时给 admin 加 `type-check`**——目前 admin 在 CI 里连类型检查都没有。

**并一并立起「CI 红即阻断」的纪律。**

> 这条比补任何一条测试都重要。已实测的事实：**CI #4 的 frontend job 已经报了
> 10 个 AppV2 错误，`origin/main`（`3aea2c2`）本身就是红的。**
> 主分支红着却没人修，说明**补再多测试也会被无视**——因为套件已经失去了否决权
> （这正是 Q1 那个核心问题的根因）。
>
> 所以 T01 不只是「加一个 job」，它修的是**让测试重新拥有否决权**这件事。

### 为什么

- 已核对可用：`frontend_admin/package.json` 的 `test:unit` 是 `vitest run`
  （已自带 run，不像 frontend 那样是 watch 模式），CI 可直接调，无需传 `--run`。
- 114 个用例是已经写好的资产，现在的状态是资产闲置。

### 代价

admin 进 CI 后，admin 的红灯会开始挡 push——这正是目的。

### 落地清单

- `.github/workflows/ci.yml` 加约 15 行（比初估的 10 行多 5 行，多在这条 type-check）。

---
