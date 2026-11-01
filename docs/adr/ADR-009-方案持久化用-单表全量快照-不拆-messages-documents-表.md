# ADR-009：方案持久化用「单表全量快照」，不拆 messages / documents 表

> 源：v2 层（原 `docs/architecture/v2/ADR.md`，已并入本目录）｜ [索引](./README.md)

**状态**：Accepted（已落地，P1 实施）

### Context

原路线图 P1 计划建 `conversations` / `messages` / `documents` 三张表。但 V2 前端的状态模型本来就是
**一份全量快照** `SessionDataV2`（`viewState` / `entryMode` / `summary` / `messages` / `conversationId`
/ 四份文档内容）。拆表意味着每次自动保存都要做差集同步，而它带来的收益（按消息 / 文档粒度查询）
**当前没有任何需求用到**。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. 单表 `solutions` 存全量 JSON + 冗余摘要列** | 保存就是一次 UPSERT，天然契合前端快照模型；防抖自动保存实现简单 | 无法按消息粒度查询统计；单行较大（一份 PRD 数十 KB） |
| B. 三表范式化 | 查询灵活，可做消息级统计 | 每次保存做差集同步；自动保存逻辑复杂；当前无查询需求 |
| C. 每方案一个 JSON 文件 | 最简单 | 无列表 / 排序 / 权限过滤能力，与「方案列表页」需求直接冲突 |

### Decision

**选 A。** `solutions(id, user_id NOT NULL 外键, title, mode, stage, data, created_at, updated_at)`，
其中 `data` 是整份 `SessionDataV2` 的 JSON 文本。

`title` / `mode` / `stage` 冗余成独立列，是为了**侧边栏列表不反序列化大 JSON** 就能渲染——
一份快照含三份文档正文，几十 KB 起步，列 20 条时差别很明显。这三个冗余列由前端随快照一起
提交（前端才理解 `product_name` 该从 `summary` 还是 `quickInput` 取），后端只做 trim 与长度校验；
后端**不解析 `data` 的内容**，保持「不透明快照」这个好处。

配套：
- 越权访问返回 **404 而非 403**——403 会泄露「这个资源确实存在」这一信息。
- 数据隔离**在 Repository 层强制 `owner_id` 过滤**，而不是靠每个路由记得检查：漏一次就是越权，
  不能把安全边界寄托在「每个写接口的人都记得」。Repository 方法**不接受 `owner_id=None`**，
  不给「不过滤」留逃生口。
- **方案「有内容了才建」**：快照的内容字段（`summary` / `messages` / `quickInput` /
  `debugPrdText` / 三份文档）任一非空时才 `POST` 创建，随后 `router.replace` 到 `/v2/{id}`；
  此后一律 `PUT`。因此保存天然分**创建 / 更新两条路径**（5 个接口而非 4 个）。
  代价是用户敲一个字就走会留下一项，靠侧边栏删除解决；收益是「填了半天刷新全丢」不会发生。
- **不改动 `conversation_service` 的内存 dict**：对话进行中的临时态仍在内存，只在用户保存 /
  进入下一阶段时落库，避免把流式生成的热路径绑上 DB 写。
- **列表不做独立页面**，改常驻左侧边栏（顶部「新建任务」+ 下方列表）。列表是工作台的一部分，
  不是另一个目的地。
- **顺带删掉** `stores/workflow.ts` 的 localStorage 快照：后端是唯一权威，留离线副本只会带来
  「恢复时该听谁的」的歧义。但它的快照校验与降级逻辑保留，改用于后端恢复。
- 接口前缀用 **`/api/solutions/*` 而非 `/api/session/*`**：引入登录后，「session」会和
  「登录会话（auth session）」语义撞车，两者是完全不同的东西。前端 service 文件同步命名
  `solutionService.ts`。

### Consequences

- 变容易：自动保存（防抖 UPSERT）实现直接；列表页查询轻量；权限只在一处强制。
- 变困难：将来若要「跨方案搜索某句话」需要扫描 JSON（届时再考虑 FTS5 或拆表）。
- 中立：单行体积可控——SQLite 单行上限远超需求，且少量人使用的场景下方案数量级有限。

---
