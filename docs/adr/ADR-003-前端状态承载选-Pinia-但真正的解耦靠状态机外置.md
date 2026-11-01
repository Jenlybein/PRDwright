# ADR-003：前端状态承载选 Pinia，但真正的解耦靠状态机外置

> 源：v2 层（原 `docs/architecture/v2/ADR.md`，已并入本目录）｜ [索引](./README.md)

**状态**：Accepted（已实施：`stores/workflow.ts` 状态机 + `stores/auth.ts` 登录态）

### Context

`App.vue` 1305 行里，状态（三份文档、消息、会话 ID、流式缓冲、错误提示）与逻辑（状态机、持久化、生成流程）混在一起。拆分的收益主要来自**逻辑外置**，而不是「换成某个状态管理库」——如果只是把 `ref` 搬进 store，巨石问题依然存在。

`pinia` 已在 `package.json` 里但从未使用。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. Pinia store** | 依赖已装、有 devtools、跨视图共享天然、易测、可做持久化插件 | 多一层概念；团队需熟悉 |
| B. `useSession()` composable + provide/inject | 更轻、无额外依赖、Vue 原生 | 跨组件调试体验差；多个组件实例时作用域易踩坑 |
| C. 维持组件内 ref | 不用改 | 巨石问题不解决 |

### Decision

**选 A，但顺序很重要**：先做 `workflow.ts` 把状态机抽成纯函数并配单测（这一步贡献 80% 的收益），再让 Pinia store 只做**状态容器 + 调用 workflow 的纯函数**。store 里不写流程判断。

若实践中觉得 Pinia 过重，退回方案 B 的成本很低——因为逻辑已经在 `workflow.ts` 里，与状态容器解耦。

### Consequences

- 变容易：跨视图共享、devtools 调试、状态机单测。
- 变困难：多一层文件；需要约定「store 不放业务逻辑」并靠 review 维持。
- 中立：`vue-router` 的取舍与本文无关，单独见 ADR-006（2026-10-13 已修订为「引入，但只做页面级导航」）。

---
