# AGENTS.md

本文件是 agent 进入这个仓库的入口。读它、读 [`docs/agents/conventions.md`](./docs/agents/conventions.md)、读 [`GLOSSARY.md`](./GLOSSARY.md)，再动手。

**项目**：PRDwright —— 用大模型生成结构化 PRD 的本地优先平台。输入一段需求，管线走「需求澄清 → 大纲 → 分章撰写 → 质量审核」，产出可编辑、可迭代的 PRD、接口文档与提示词。模型接入、密钥、系统参数都在独立的管理后台里管。人读的说明在 [`README.md`](./README.md)。

## 仓库速查

| 目录 | 技术栈 | 端口 | 说明 |
| --- | --- | --- | --- |
| `backend/` | Python 3.12 · uv · FastAPI · SQLModel · SQLite/MySQL | 8000 | 全部业务逻辑、鉴权、持久化、LLM 管线 |
| `frontend/` | Vue 3 · Vite | 5173 | 用户端 |
| `frontend_admin/` | Vue 3 · Vite | 5174 | 管理端（与用户端零共享，`npm install` 需 `--legacy-peer-deps`） |
| `skills/` | —— | —— | 生成管线的技能包（`prd-generator` 等），是管线知识的唯一来源 |
| `scripts/keygen/` | Python | —— | 主密钥生成与轮换 |
| `docs/` | Markdown | —— | 架构文档 · 约定 |

```bash
cd backend && uv sync && uv run pytest     # 后端：只准用 uv，没有 requirements.txt
cd frontend && npm run test:unit           # 用户端
cd frontend_admin && npm run test:unit     # 管理端
```

## 不可违反的纪律

1. **注释与提交信息用中文，标识符用英文。**
2. **提交与推送前必须经用户确认**（先 `git add -An .` 预演）。agent 不自行 commit。
3. **只用 uv**；删除依赖也走 `uv`。
4. **文档里写出的每个路径，`ls` 必须能看见** —— 本项目两次出现「报告写入成功但文件次日消失」，写完必须 `ls` + `git status` 复核。
5. **架构文档正向写**：目标 → 范围（含「本期不做」）→ 设计 → 契约 → 取舍。禁止写成「盘点了哪些缺口」。
6. 一次改动要**收口**：代码、架构文档、路线图三者同步，谁都不滞后。

完整的工程约定（分层边界、迁移纪律、SSE 契约、版本链语义、测试陷阱、部署硬约束）在 [`docs/agents/conventions.md`](./docs/agents/conventions.md)。

## Agent skills

### Issue tracker

issue 与规格住在 GitHub Issues（`Jenlybein/PRDwright`，public），一律用 `gh` CLI 读写。见 [`docs/agents/issue-tracker.md`](./docs/agents/issue-tracker.md)。

### Triage labels

五个固定角色，标签字符串与角色同名：`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`。见 [`docs/agents/triage-labels.md`](./docs/agents/triage-labels.md)。

### Domain docs

单上下文：根目录一份 `GLOSSARY.md`。架构现状只有一版 [`docs/architecture/ARCHITECTURE.md`](./docs/architecture/ARCHITECTURE.md)（含「取舍」节与契约）；**架构决策**单独归档在 [`docs/adr/`](./docs/adr/)，编号从 0001 重新开始（目前只有 0001）。[`docs/architecture/README.md`](./docs/architecture/README.md) 是时间线真源，[`docs/architecture/roadmap.md`](./docs/architecture/roadmap.md) 是现状排期。见 [`docs/agents/domain.md`](./docs/agents/domain.md)。
