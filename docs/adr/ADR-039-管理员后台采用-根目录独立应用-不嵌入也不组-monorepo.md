# ADR-039：管理员后台采用「根目录独立应用」，不嵌入也不组 monorepo

> 源：v4 层（原 `docs/architecture/v4/ADR.md`，已并入本目录）｜ [索引](./README.md)

### Status

Accepted（2026-10-21）

### Context

需要新建平台管理员后台。三个候选：

1. **嵌入现有 `frontend/`**：加 `/admin/*` 路由与页面，作为一个 feature module；
2. **根目录单开 `frontend_admin/`**：独立 `package.json`、独立 Vite 应用；
3. **pnpm monorepo**：`frontend/` 下拆 `userpage` / `adminpage` 两个应用 + `packages/shared`。

讨论中先后提出过三类顾虑：

- **安全**：「一起放着是不是容易被黑客找到后台」；
- **开发冲撞**：「一起放着会不会互相冲撞」；
- **工程成本**：monorepo 的双份工具链与 shared 包维护。

其中「隐藏路径能挡住攻击者」这一点经核实不成立：SPA 的路由本就打进 JS 包，
攻击者直接扫 `/api/*`，前端怎么组织都不改变 API 的暴露面；且两个前端只要请求同一个 API 源，
会话 Cookie 本来就通用。真正的安全边界是**服务端 `require_admin`**，
与代码是否分开放置无关。

### Decision

取 **方案 2（根目录独立应用 `frontend_admin/`）**。

- 与 `backend/`、`frontend/` 并列，独立构建、独立部署。
- 两个前端共享同一后端 API 源，因此共享同一套会话 Cookie，**不做任何登录态桥接**。
- 管理员端**不 import** 用户端任何模块；自带一份薄 HTTP 封装与类型。
- 服务端对所有 `/api/admin/**` 强制 `require_admin`（这是唯一硬防线）。

### Consequences

**变容易**

- 独立构建与部署：管理员端可单独发布、可放独立子域或内网（这是「分离」唯一能带来
  真实安全增益的地方，且它靠的是部署边界，不是代码位置）。
- 硬隔离：TypeScript/Vite 天然阻止跨应用 import，不会因手滑把管理员组件带进用户端。

**变难 / 代价**

- 多一套工具链（Vite / 依赖 / CI），以及少量 HTTP 封装的重复。
- **git 冲突并不会因此减少**——两个应用仍在同一仓库、同一段提交历史；
  真要消除版本控制层面的冲撞只能拆独立仓库，那代价远大于收益。
- 部署时要处理两个 SPA 共存（反代分路径或独立子域）与 `cors_origins` 放行。

---
