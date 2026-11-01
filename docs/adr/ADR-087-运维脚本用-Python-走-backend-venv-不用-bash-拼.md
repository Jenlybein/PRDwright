# ADR-087：运维脚本用 Python（走 `backend/.venv`），不用 bash 拼 CLI

> 源：v7 层（原 `docs/architecture/v7/ADR.md`，已并入本目录）｜ [索引](./README.md)

**状态**：已定（v7 新增，T17.0 落地）。

### 背景

v5 起草时定的是三个 bash 脚本：`backup_db.sh` / `smoke.sh` / `deploy.sh`。
它们要用的能力是：

- 备份：对 SQLite 执行 `VACUUM INTO`，然后**打开产物做自检**（`integrity_check`、
  读 `alembic_version`、算 sha256）；
- 冒烟：发 HTTP 请求、解析 JSON、判断状态码与响应头；
- 发布：串行执行一串步骤，任一步失败即中止，并且**要能判断「这次发布是否包含迁移」**。

### 决策

**全部用 Python**，落点 `scripts/deploy/`，运行方式是
`/opt/prdwright/backend/.venv/bin/python scripts/deploy/xxx.py`：

| 脚本 | 职责 |
| --- | --- |
| `backup.py` | `VACUUM INTO` + 主密钥归档 + `manifest.json` + 自检；失败非 0 退出 |
| `restore.py` | 从备份恢复到一个**指定目标路径**（不自动起服务，不自动覆盖生产库） |
| `release.py` | 按 ADR-057 的六步发布；每步失败即中止并打印恢复指引 |
| `smoke.py` | `--basic` / `--deep` 两档冒烟；非 0 即失败 |

### 理由

1. **零新增依赖**。`sqlite3`、`hashlib`、`json`、`urllib`、`http.client` 全在 stdlib；
   运行环境就是后端自己的 `.venv`（`uv sync` 已经装好），不需要目标机额外装 `sqlite3` CLI、
   `jq`、`curl` 的某个特定版本。
2. **备份自检只能用代码做**（ADR-058 的 v7 修订）。bash 里做 `PRAGMA integrity_check`
   要 `sqlite3 ... | grep ok`，而 `grep` 成功不代表文件完整；读 `alembic_version` 也同理。
   用 Python 是 `conn.execute(...).fetchone()`，失败会抛异常，不会静默通过。
3. **发布脚本要判断「有没有新迁移」**。`sqlite3` 读 `alembic_version` + 比对
   `migrations/versions/` 的 head，用 bash 写要三次字符串处理，用 Python 是两行。
   这个判断决定了回滚时要不要恢复数据（ADR-057 §10.2）。
4. **失败语义统一**：Python 的 `SystemExit(1)` 与异常栈比 bash 的 `set -e` 更容易
   做到「在正确的位置停」，而 `set -e` 在管道与函数里有一堆反直觉的例外。

### 被否决的方案

| 方案 | 否决理由 |
| --- | --- |
| 纯 bash（v5 起草时的做法） | 能用，但自检与 JSON 解析都要靠外部工具；而且「备份成功」的判据会退化成「文件存在且非空」——恰恰是 ADR-058 要否掉的那种弱判据 |
| 引入 `boto3` / `click` / `requests` 等库 | 运维脚本的依赖也会变成「发布时要先装它」。stdlib 够用，就不引 |
| 放到 systemd timer 里跑备份 | 备份这件事需要**看得见失败**。timer 的失败只进 journal，而发布脚本里的备份失败必须当场中止发布。两者可以共存：日常备份用 timer 调同一个 `backup.py` |

### 代价

- 脚本不能「一行命令贴到聊天窗口让别人执行」——得带解释器路径。用
  `#!/opt/prdwright/backend/.venv/bin/python` 的 shebang 加 `chmod +x` 解决。
- `restore.py` 必须**故意**做得很笨：只接受显式的 `--to` 参数，不猜路径、不自动停服务、
  不自动覆盖。恢复是危险动作，脚本的价值在于「不替人做决定」。

### 落地清单

- `scripts/deploy/{backup,restore,release,smoke}.py`；
- `deploy/backend.env.example`（生产启动项模板，逐项带理由）；
- `deploy/systemd/prdwright.service`、`deploy/nginx/prdwright.conf`（可复制到目标机）；
- 每个脚本 `--help` 能自解释，且**不依赖当前工作目录**（路径全部相对脚本自身解析）。

---
