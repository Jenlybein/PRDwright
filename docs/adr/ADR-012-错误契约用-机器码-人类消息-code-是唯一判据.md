# ADR-012：错误契约用「机器码 + 人类消息」，`code` 是唯一判据

> 源：v2 层（原 `docs/architecture/v2/ADR.md`，已并入本目录）｜ [索引](./README.md)

**状态**：Accepted（2026-10-14 已落地，`7be5989`；R0-T5）

### Context

前端要靠**匹配后端返回的中文文案**来决定行为：

```ts
// frontend/src/composables/useClarificationChat.ts:128（改造前）
if (!conversationId.value || !message.includes('会话不存在')) throw error
```

这段判据有两个致命性质：

1. **静默失效**：后端只要把「会话不存在」改一个字，前端降级逻辑（清掉失效会话 ID 后无状态重试）就不再触发，用户直接卡死，**且没有任何测试会发现**——测试不会去改后端文案。
2. **来源被误判**：按表面看它像是在匹配 HTTP 响应的 `detail`，实测**该文案来自 HTTP 200 的 SSE 流内 `error` 事件**（`conversation_service.py:487` 的 `StreamEvent(type="error", ...)`）。只给 REST 响应加码**完全解决不了这条链路**。

同时后端错误有 **4 个产出来源**，只改一处会漏：`HTTPException` → `{"detail":...}`、`RequestValidationError`（422，`detail` 是列表）、中间件裸写的 ASGI JSON（`deps.py::_send_unauthorized`）、SSE `error` 事件。全仓库当时 grep `error_code` 零命中。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. `detail` 一字不改，并列新增 `code`** | 向后兼容：现有 93 个后端测试与前端 `readDetail()` 全部不受影响；`detail` 从此可随时改写而不影响逻辑 | 响应体多一个字段（冗余但无害） |
| B. 改成 `{"error": {"code":..,"message":..}}` 规范结构 | 结构上更"标准" | 推翻现有 93 个测试与前端读取逻辑，**收益与 A 相同**，纯破坏性 |
| C. 维持字符串匹配，只加测试 | 零改动 | 测试无法阻止后端改文案；契约仍然脆 |

### Decision

**选 A。**

- 响应体：`{"detail": <原文一字不改>, "code": "<UPPER_SNAKE_CASE>"}`；422 的 `detail` 仍保持 FastAPI 的列表结构。
- SSE `error` 事件：`code` 放在**顶层字段**（不塞进 `data`），`content` 仍是中文原文。
- 后端单一真源 `core/errors.py`（`ErrorCode` 8 个码 + `AppError` + `DEFAULT_STATUS` + `error_payload`）。放 `core/` 而非 `api/`，避免 services 反向依赖 api。
- **必须注册自定义异常处理器**：`HTTPException(detail={"detail":..,"code":..})` 会被 FastAPI 渲染成 `{"detail":{"detail":...}}` 嵌套结构，`register_error_handlers(app)` 是唯一能「一处保证所有错误响应都带 code」的做法。
- 前端单一真源 `services/errors.ts`（`BACKEND_ERROR_CODES` / `ApiError` / `isApiError` / `toErrorCode`）。**必须放独立模块**：`AppV2.spec.ts` 用 `vi.mock('../services/api', ...)` 整体替换 api 模块，若 `ApiError` 定义在 `api.ts` 里，`instanceof` 会抛 `TypeError`。
- 只定义 **8 个真正用得上的码**，不搞大而全枚举（错误码体系最容易膨胀成没人维护的摆设）。
- 已做对的部分**不动**：401 → `UnauthorizedError`、404 → `SolutionNotFoundError` 本就基于 HTTP 状态码，是正确的。

### Consequences

- 变容易：后端文案可随时改写（产品措辞调整不再牵动前端逻辑）；新增机器判据只需加码。
- 变困难：新增错误码必须**前后端同步**，否则不变量测试 `test_frontend_error_codes_match_backend`（正则解析 `errors.ts` 断言与后端枚举**双向严格相等**）立刻报红。这是刻意的——漂移应在 CI 暴露，而非运行时。
- 中立：框架级 404/405（如访问不存在的路径）仍无 `code`——为守住 8 个码的最小集合，未注册 `StarletteHTTPException` 兜底处理器。已知边界，接受。
- 防回退由 4 条不变量测试守护（每个错误响应带 `code`、前后端码值相等、前端源码禁中文串判断、`api/` 层禁裸 `HTTPException`），并用**变异测试**验证守卫不空转（删码 / 加裸 `HTTPException` / 加中文匹配 / 退回字符串判据 → 均正确报红）。

---
