# ADR-013：流式编排收敛到 `stream_reply.py` 一处

> 源：v2 层（原 `docs/architecture/v2/ADR.md`，已并入本目录）｜ [索引](./README.md)

**状态**：Accepted（2026-10-14 已落地，R0-T3）

### Context

「把模型流变成 SSE 事件」这件事在系统里有 **四份实现**（2026-10-14 实测）：

| # | 位置 | 规模 | 说明 |
| --- | --- | --- | --- |
| 1 | `conversation_service._stream_reply` | 366–408（44 行） | 带 `on_finish` 回调 |
| 2 | `document_service._stream_reply` | 626–668（48 行） | 无 `on_finish` |
| 3 | `prd_section_writer._error_event` | 775–786 | **第三份**「异常 → error 事件」转换，其注释自认「与 `document_service._stream_reply` 的口径一致」——**靠注释保证一致** |
| 4 | `conversation_service._extract_text` | 101–118 | 与 `llm_utils.extract_text`（21 行）**逐行相同**的副本 |

先前已抽出 `stream_guard.py::ensure_not_truncated`（截断检测），但**核心编排的重复没有消除**。三处「异常 → error 事件」各自维护的后果是：改错误码口径要改三处，漏一处则前端对同一种故障收到两种分类。

两份 `_stream_reply` 的差异仅三处：`on_finish`（conversation 用于回写历史，452/524 两处调用）、文本提取用哪个函数、截断检查放在 `_astream_text` 内还是内联（**两者都落在 `try` 内 → 语义等价**）。

> 附带更正：旧文档一度把「`conversation_service.py:366` / `document_service.py:626`」的**行号**当成行数，将本任务描述为「365 / 625 行的大手术」。真实规模约 90 行。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. 新建 `services/stream_reply.py`，三处改为调用** | 单一实现；错误口径不可能漂移；`stream_guard.py` 保持只管截断检测 | 多一个模块 |
| B. 并入现有 `stream_guard.py` | 不新增文件 | `stream_guard` 的语义是「截断检测」，混入编排会让该文件失去单一职责 |
| C. 抽成基类/混入由两个 service 继承 | 不改调用点 | 引入继承只为复用一段函数，收益不抵复杂度；且两 service 并无真实 is-a 关系 |

### Decision

**选 A。** 新增 `services/stream_reply.py`，提供统一的事件流编排（`messages` / `status_text` / 可选 `on_finish` / `extra_data` / 错误码）与**唯一的**「异常 → error 事件」转换，供三个 service 复用；`stream_guard.py` 保持只管截断检测。

实施顺序是硬约束：**先写对照测试（`backend/tests/test_stream_reply.py`，用现有 `FakeLLM` 范式）钉死当前行为，再统一**。理由是两份 `_stream_reply` 当时**零直接单测**、`stream_guard` 无专属测试文件，只有 `test_prd_section_stream.py` 5 例覆盖分章节那条流——没有网不能动。

对照测试必须覆盖：事件序列 `status → chunk* → done`；异常 → `error` 事件（`code=LLM_UPSTREAM_ERROR`）**且之后不再发 `done`**；截断 → `error` 事件；`on_finish` 在 `done` 之前调用且**仅在成功时**调用；`extra_data` 透传进 `done.data`。conversation / document 两条路径参数化各跑一遍。

**不做**：不改事件序列、文案、错误码（对外契约零变化）；不合并 `_collect_reply`（语义是"收拢成整段文本"供审核解析 JSON，不是事件流）；不合并 `prd_section_writer._stream_text` / `_collect`（分章节有自己的 `stage`/`part` 语义，硬并会抹平差异）。

### Consequences

- 变容易：错误口径只有一处；加 `request_id` 打点（R2-T7）、加 token 计量（R1-T5）都有了唯一落点——这是本决策**最大的隐藏收益**。
- 变困难：统一实现的参数列表要同时满足三个调用方，若将来差异扩大需警惕「配置项膨胀」——届时宁可拆成两个具名函数，也不要往一个函数里堆开关。
- 中立：`document_service._collect_reply` 与 `prd_section_writer._collect` 保留为具名方法（语义是"收拢成整段文本"，不是事件流），但内部改为复用共享的 `collect_reply`，不再各自维护 `last_chunk` / 截断检查那段循环。

### 实施记录（2026-10-14）

- **先差分、后替换**：改动前用一次性脚本对旧实现与新实现跑了 **12 组场景**（正常多块 / 单块 / 截断 `length` 与 `max_tokens` / 空输出 / 中途异常 / 结构化列表分块 / 省略 `status_text`），比对逐事件的 `(type, content, data, code)`，**全部一致**后才删除旧代码（脚本已删除，不入库）。
- 删除：`conversation_service._stream_reply` / `_astream_text` / `_extract_text`、`document_service._stream_reply`、`prd_section_writer._error_event` 包装；6 处调用点改为直接调用共享函数。
- 新增 `backend/tests/test_stream_reply.py`（10 例）：事件序列、异常转 `error` 且不发 `done`、`on_finish` 仅成功时调用、结构化分块摊平、截断抛错、默认错误码；外加一条**源码扫描不变量**——`services/` 下（除 `stream_reply.py`）不许再出现 `async def _stream_reply` / `def _extract_text` / `def _astream_text`，防止重复悄悄复活。
- 回归：后端 **117**（107 + 10）、前端 **222**、`npm run build` 全绿。

---
