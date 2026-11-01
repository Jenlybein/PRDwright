# ADR-061：集成测试只覆盖「传输层即契约」的接口，止于 SSE 一族

> 源：v5 层（原 `docs/architecture/v5/ADR.md`，已并入本目录）｜ [索引](./README.md)

> 状态：**已推迟**（用户决定集成测试与系统测试本期都不做，之后再做）。
> 本条的判定规则保留，作为将来重启这项工作的依据；
> 本期的处理见文末「推迟后的替代动作」。

### 背景

integration 为 0，e2e 为 0。但本项目是单进程 + 单 SQLite + 单 app，
没有多服务、没有消息队列、没有跨进程边界。

### 决策

判定规则：只有当「经过 HTTP 层」本身会改变可观测行为时，才写集成测试。

- 对普通 REST 接口，HTTP 只是搬运工，测 service 就等于测了集成 → 不写；
- 对 SSE，分帧节奏、响应头、连接语义本身就是契约，绕开 HTTP 就等于没测 → 必须写。

按此规则扫描，命中的只有 SSE 一族：`/api/jobs/{id}/stream` 与 `conversation/*-stream`。
加满这一族就停。

明确不做反面：不给每个接口补一条 HTTP 冒烟。

### 为什么

这个仓库里不存在天然的集成第二层，硬造一个造出来的就是 HTTP 冒烟
（40 多个用例，覆盖的是 TestClient 自己）。

### 代价

`X-Accel-Buffering` 这类头在 TestClient 下即使断言了，也不能证明 nginx 真的不缓冲
——仓库里没有 nginx 配置可对。

> ⚠️ 因此本 ADR 附带一条边界声明：SSE 集成测试守的是「后端发出的头是对的」，
> 不是「生产链路是通的」。 后者要等 v6 容器化之后才有靶子。
> 同理，真实断线重连在 jsdom 下不做自动化覆盖（jsdom 无真实网络中断）。

### 落地清单（推迟后的替代动作）

本期**不建** `backend/tests/integration/` 目录，也不新增集成层工程，整条 T05 推迟到之后。

但保留一个最小例外，理由是不留它就有个「生产必炸、本地必绿」的洞：

- 在**现有的** HTTP 接口测试里补 3 条响应头断言
  （`Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`），
  复用 `test_chat_http_flow.py` 里已有的 `client.stream(...)` 写法；
- 不新建文件、不建目录 —— 就是那个文件里多 3 行 `assert`。

这**不构成**集成测试：它仍在 TestClient 同进程内，验的是「后端发出的头对不对」，
不是「Nginx 有没有真的关掉缓冲」（后者要等容器化有靶子才验得了）。

原先计划的第 4、5 条（`Connection: keep-alive`、跨 chunk 半帧拼装 + `[DONE]` 哨兵）
一并推迟 —— 它们需要构造跨 chunk 的流，属于真正的传输层测试。

---
