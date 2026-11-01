# ADR-007：认证用服务端不透明会话 token + HttpOnly Cookie，不上 JWT

> 源：v2 层（原 `docs/architecture/v2/ADR.md`，已并入本目录）｜ [索引](./README.md)

**状态**：Accepted（2026-10-13 已实施）

### Context

需要自建账号密码登录（少量人使用）。争议点只有一个：**会话凭据用无状态的 JWT，还是有状态的服务端 token**。

曾经考虑过 JWT，动机是「将来可能扩大用户范围」。但扩大用户范围真正带来的是**多实例部署**压力，
而当前连单机并发都不存在；为了一个尚未出现的约束提前引入密钥管理与吊销复杂度，是典型的过度设计。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. 服务端不透明 token + 会话表 + HttpOnly Cookie** | 登出**即时失效**（删一行即可）；无签名密钥要管理；密码学攻击面小 | 每次请求多一次会话表读；多实例部署时会话表要共享 |
| B. JWT（无状态） | 无需查库，天然适配多实例 | 登出无法即时失效（要黑名单，等于又变回有状态）；密钥轮换复杂；当前无多实例需求 |
| C. 托管认证（邮箱 / 手机验证码） | 不用存密码 | 引入外部依赖与费用；本地开发必须联网；离线不可用 |

### Decision

**选 A，但把签发 / 校验 / 吊销封成 `TokenIssuer` 协议**（`issue` / `verify` / `revoke`），
当前实现是 `OpaqueSessionIssuer`。将来若真要换 JWT，只换一个实现类，业务代码零改动。

配套：
- 随机 token 用 `secrets.token_urlsafe(32)`，库里**只存 SHA-256**，不存明文——拖库也拿不到可用凭据。
- Cookie 设 `HttpOnly` + `SameSite=Lax` + `Path=/`；`Secure` 由 `AUTH_COOKIE_SECURE` 控制（本地 HTTP 开发默认关）。
- 密码用标准库 `hashlib.scrypt` + 每用户独立 salt，比对用 `hmac.compare_digest`；**零新增依赖**。
- 用户不存在时照常执行一次 dummy scrypt 再返回，抹平时序差异；登录失败统一文案「用户名或密码错误」，防账号枚举。
- 开放注册但需邀请码（`invite_codes` 表，支持 `max_uses` / `expires_at`）；核销走**单条原子 UPDATE**
  靠 `rowcount` 判定，防并发超发。注册顺序为「查重 → 核销邀请码 → 建号」，避免用户因重名白亏一次额度。

### Consequences

- 变容易：登出即时生效（已有测试覆盖：登出后偷回 cookie 仍 401）；无密钥轮换负担；`AUTH_ENABLED=false`
  时回落内置 dev 用户，保证 `user_id` 恒非空，将来开启鉴权**零迁移**。
- 变困难：每次请求一次会话表读（SQLite 本地读，成本可忽略）；横向扩展时会话表需共享——
  那正是换掉 `TokenIssuer` 实现的时机。
- 中立：不做角色权限体系。少量人场景，owner 过滤足够（见 ADR-009）。

---
