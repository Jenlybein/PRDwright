# PRDwright

> 用大模型生成结构化产品需求文档（PRD）的**本地优先**平台，带独立的管理员后台与可审计的运营能力。

输入一段需求或想法，PRDwright 走一条多阶段管线（需求澄清 → 大纲 → 分章撰写 → 质量审核），
产出一份可编辑、可迭代的 PRD。模型接入、密钥、系统参数都在**管理后台**里管，数据库用 SQLite，
整栈可单机部署。

---

## 仓库结构（monorepo）

| 目录 | 技术栈 | 端口 | 说明 |
| --- | --- | --- | --- |
| `backend/` | Python 3.12 · [uv](https://docs.astral.sh/uv/) · FastAPI · SQLite | 8000 | 全部业务逻辑、鉴权、持久化、LLM 管线 |
| `frontend/` | Vue 3 · Vite | 5173 | 用户端：写 PRD、看生成进度、编辑产物 |
| `frontend_admin/` | Vue 3 · Vite | 5174 | 管理端：用户 / 邀请码 / 审计 / 任务监控 / 模型配置 |
| `skills/` | —— | —— | 生成管线使用的技能包（`prd-generator` 等） |
| `scripts/keygen/` | Python | —— | 主密钥生成与轮换脚本 |
| `docs/architecture/` | Markdown | —— | 架构说明与决策记录（ADR） |

---

## 快速开始

### 前置依赖

- Python ≥ 3.12
- Node ≥ 22.18（或 ≥ 24.12）
- [uv](https://docs.astral.sh/uv/)（Python 包管理）

### 1. 后端

```bash
cd backend

# 首次：安装依赖（uv 会按 uv.lock 装好，含 cryptography）
uv sync

# 首次：生成主密钥（只需一次；详见下节「密钥与配置」）
uv run python ../scripts/keygen/generate_keys.py

# （可选）本地配置：默认值即可起服务，不写任何一行也能跑
cp .env.example .env

# 启动（带热重载）
uv run uvicorn backend.main:app --reload
```

启动后接口文档在 <http://localhost:8000/docs>。

### 2. 用户前端

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

### 3. 管理后台

```bash
cd frontend_admin
npm install --legacy-peer-deps   # 该工程依赖需用 legacy-peer-deps 解析
npm run dev                     # http://localhost:5174
```

首次启动会用 `INVITE_CODE`（默认 `123456`，见 `.env.example`）播种管理员；
之后在管理后台「模型配置」里录入要用的模型供应商与密钥，前端才能开始生成。

---

## 密钥与配置（重要）

模型密钥的存放方式在 **ADR-053** 中定稿，是这套部署最该先理解的一点：

- **密钥不再放在 `.env`**。它改为在管理后台「模型配置」页录入，由服务端用一把主密钥
  加密（Fernet）后以**密文**存进数据库（`model_providers.value_token`）。
- **主密钥**位于 `backend/secrets/master.key`（32 字节，文件权限 600，已在 `.gitignore`）。
  它**不进数据库、不进 `.env`**；生成命令见上方。
  - ⚠️ 主密钥丢了 = 所有已录入的密钥都解不开。备份时请把它与数据库**分开**存放。
  - 轮换主密钥用 `generate_keys.py --rotate`（先重加密库里全部密钥值，**最后**才替换文件）。
- **`.env` 只放启动项**（端口 / 库路径 / 日志 / 鉴权开关等，决定「进程怎么起来」的项）。
  具体见 `backend/.env.example`，全程注释、默认值即可起服务。
- 本地 vLLM / Ollama 这类**不要密钥**的接入点：在后台密钥框填占位值 `not-needed`
  （OpenAI 兼容客户端要求 `api_key` 非空）。
- 查看明文密钥是**独立动作**（`reveal-key` 接口），且会写入审计日志；列表里始终只回
  `key_present`（已配置 / 未配置）布尔，绝不返回密钥值。

> 如果你是从老版本升级：旧 `.env` 里的 `ARK_API_KEY` 等已经不被读取，请到后台重新录入，
> 确认能正常生成后再删除本地 `.env` 里那几行密钥（那是你本地的唯一副本，删前务必先入库）。

---

## 测试

```bash
# 后端（Python）
cd backend && uv run pytest

# 用户前端
cd frontend && npm run test:unit

# 管理后台
cd frontend_admin && npm run test:unit
```

后端与两个前端的类型检查分别用 `uv run pytest` 自带的静态路径，以及各自的
`npm run type-check`（即 `vue-tsc --build`）；构建用 `npm run build`。

---

## 文档

- 架构总览：`docs/architecture/v4/ARCHITECTURE.md`
- 决策记录（ADR，含 ADR-053 密钥加密入库）：`docs/architecture/v4/ADR.md`
- 路线图：`docs/architecture/v4/roadmap.md`
- 历史版本：`docs/architecture/v1` ~ `v3`

---

## 安全须知

- `.env`、`backend/secrets/`（含 `master.key`）、本地 `*.db` 全部在 `.gitignore` 中，**不入库**。
- 不要向仓库提交任何真实密钥；密钥只经管理后台录入，明文只在内存中短暂存在。
- 管理后台的写操作（改配置、切供应商、录入/查看密钥、管用户等）都会写入审计日志。
- 数据库导出（账号数据 ZIP）刻意**不包含**全局配置表，密钥密文不会随数据包流出。
