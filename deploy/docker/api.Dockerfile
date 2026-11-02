# PRDwright 应用镜像：API 进程（FastAPI + 技能包），单 worker、非 root、主密钥缺失即拒启动
#
# 构建上下文是**仓库根**（要同时看见 backend/、backend/migrations/、skills/、scripts/keygen/）：
#
#     docker build -f deploy/docker/api.Dockerfile -t prdwright-api:local .
#
# 根目录的 `.dockerignore` 负责把 .venv / node_modules / 本地 *.db 与 -wal/-shm /
# backend/.env / **backend/secrets/** / __pycache__ / .git 挡在上下文之外，与 web.Dockerfile
# 共用同一份。其中 `backend/secrets/` 那条是**安全边界**而不是文件卫生：主密钥一旦进了
# 镜像层，镜像被谁拉走就等于密文被谁拿走了一半。
#
# 两个阶段各管一件事：builder 只装依赖（按 backend/uv.lock，可复现），runtime 只装运行时
# （venv + 源码 + 迁移 + 技能包）。最终镜像里没有 uv、没有编译器、没有任何前端源码。

# ============================================================================
# 阶段 1 —— 按锁定文件装依赖
# ============================================================================
FROM python:3.12-slim AS builder

# uv 的版本钉死：`uv sync --frozen` 的解析行为与链接方式跟版本绑定，用 latest 等于让
# 「同一份提交能不能构建出同一个镜像」随时间漂移。
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/

WORKDIR /build/backend

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# 先只拷依赖清单：依赖层单独成层，改业务代码不会重装整棵 langchain 依赖树。
COPY backend/pyproject.toml backend/uv.lock ./

# `--frozen`：只按 uv.lock 装，绝不在构建期重新解析——否则「锁住的版本」只是建议。
# `--no-dev`：pytest / xdist 属于开发依赖，不进运行时镜像。
# `--no-install-project`：项目自己不装成 site-packages 里的副本。运行时靠
#   `PYTHONPATH=/app/backend/src` 指过去。这样源码目录与 import 路径是同一份东西，
#   不会出现「venv 里装着构建时那份代码、改的源码没生效」这种镜像特有的错位；
#   代价是没有 `backend` 的包元数据，而本项目没有任何地方读它。
RUN uv sync --frozen --no-dev --no-install-project

# ============================================================================
# 阶段 2 —— 运行时：只读代码 + venv + 技能包
# ============================================================================
FROM python:3.12-slim AS runtime

# 运行用户的 uid/gid 固定：卷里的文件属主因此是个稳定数字，宿主上对得清、也对得上
# 「启动脚本校正属主」这件事。10001 而不是 1000——1000 在多数 Linux 宿主上已被占用。
ARG APP_UID=10001
ARG APP_GID=10001
# 镜像引用只为了启动脚本里那句「生成主密钥」的命令能被直接粘贴执行。
ARG IMAGE_REF=prdwright-api:local

# 用 gosu 而不是 su-exec：su-exec 是 Alpine 生态的（busybox 风格），本镜像是 Debian
# slim——要的是 glibc 的 manylinux 轮子，换 musl 就得现场编译 tokenizers / grpc 这类
# 带原生代码的依赖。gosu 在 Debian 官方仓库里就有，而且它是「降权后 **exec**」：
# 降权后的进程直接替换掉自己，于是 PID 1 就是应用本身，SIGTERM 直达 uvicorn——
# 这正是 #13 的排空要的语义（信号落在应用上，而不是落在中间那个 shell 上）。
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends gosu; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --gid "${APP_GID}" prdwright; \
    useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin prdwright

# ---------------------------------------------------------------------------
# 运行时环境变量：这一块就是与编排层（#16）之间的契约——卷挂在哪、变量叫什么，全在这里
# ---------------------------------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend/src \
    PATH=/opt/venv/bin:$PATH \
    PRDWRIGHT_SKILLS_DIR=/app/skills \
    PRDWRIGHT_SKILLS_STRICT=true \
    PRDWRIGHT_MASTER_KEY_PATH=/secrets/master.key \
    DATABASE_URL=sqlite:////var/lib/prdwright/prdwright.db \
    LOG_DIR=/var/log/prdwright \
    LOG_FORMAT=json \
    PRDWRIGHT_IMAGE_REF=${IMAGE_REF}
#
# 逐条理由：
# · `PYTHONDONTWRITEBYTECODE`：字节码在构建期已编译（`UV_COMPILE_BYTECODE`），运行期
#   不必也不许往只读的 /app 写 `__pycache__`。
# · `PRDWRIGHT_SKILLS_DIR` **显式给**，不赌 `skill_registry.skills_root()` 的向上查找。
#   那个兜底是「从模块文件向上找 `<祖先>/skills`」，对 `/app/backend/src/backend/services/`
#   来说要爬到第 5 层才命中，等于把镜像的目录层级变成隐式契约；显式变量与目录层级解耦，
#   以后挪 /app 也不用重新数父目录。副作用是好的：`/app/skills` 同时也满足向上查找，
#   两条路指同一个目录，谁先命中都不会读到另一套资产。
# · `PRDWRIGHT_SKILLS_STRICT=true`：部署态漏打包技能包要在启动时就炸，而不是某次生成
#   悄悄退回内置提示词（`skill_registry.ensure_skills` 的 fail-fast）。
# · `DATABASE_URL` 用 4 个斜杠的绝对路径形态；`LOG_DIR` 指向日志卷。两者都在卷里，
#   容器重建后还在。
# · `PRDWRIGHT_IMAGE_REF` 只为报错文案可执行（见启动脚本的密钥指引）。

# 依赖：整棵 venv 从 builder 搬过来，构建工具与 uv 留在上一阶段。
COPY --from=builder /opt/venv /opt/venv

# 只拷需要的那几棵子树。测试、前端源码、node_modules、local .env 一概不进最终镜像，
# 所以「最终镜像不含构建工具链与前端源码」是**结构上的**，不是靠人记得别拷。
COPY backend/src /app/backend/src
COPY backend/migrations /app/backend/migrations
COPY backend/alembic.ini /app/backend/alembic.ini
COPY skills /app/skills
COPY scripts/keygen /app/scripts/keygen
COPY deploy/docker/api-entrypoint.sh /usr/local/bin/api-entrypoint

# 三个挂载点先建好并落到运行用户名下：
#   /var/lib/prdwright  数据卷（SQLite 库 + WAL 的 -wal / -shm 边车）
#   /var/log/prdwright  日志卷（轮转文件）
#   /secrets            密钥卷（只读挂载，master.key 由运维一次性生成）
# 命名卷首次创建时会**继承镜像里这个目录的属主与权限**，所以「先建好 + 改属主」意味着
# 多数情况下第一次启动根本不需要 chown。启动脚本里的校正负责兜住其余情况：卷早于本镜像
# 被创建（属主 root）、从别处恢复、或下一版换了 uid。
RUN set -eux; \
    install -d -o prdwright -g prdwright -m 0750 /var/lib/prdwright /var/log/prdwright /secrets; \
    sed -i 's/\r$//' /usr/local/bin/api-entrypoint; \
    chmod 0755 /usr/local/bin/api-entrypoint; \
    chown -R root:root /app; \
    chmod -R a-w /app
# `sed` 那一步是防 Windows 检出：本仓库的 `core.autocrlf=true` 会把 shell 脚本在工作区
# 变成 CRLF，而 `#!/bin/sh\r` 在容器里等于「解释器不存在」（报错只说 not found）。
# 去掉行尾 CR 比要求每个人配好 git 属性更可靠，也不依赖构建机的配置。
# 最后两行是「代码层只读」：/app 里没有一个可写位，运行期想写也写不进去，
# 于是不存在「容器里改的东西随重建消失」这种模糊地带。可变状态只允许落在三个卷里。

EXPOSE 8000

# 镜像自带的探针只打**生死探针**：`/health` 恒 200（拿它探活等于没探），`/readyz` 是
# 编排器摘流用的判据（由库可写 + 技能可加载决定），不该由镜像内部的探针反复触发。
# 用 stdlib 的 urllib 而不是 curl：slim 里没有 curl，为了一个探针多装一个包不值得。
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1

WORKDIR /app

# ⚠️ 这里**刻意没有** `USER prdwright`：启动脚本的第一阶段必须是 root（校正卷属主），
# 之后自己 `exec gosu` 降权，降权后的进程就是 PID 1。因此
# `docker exec <容器> id` 看到的是 root（exec 用的是镜像的 User，不是进程的），
# 要验证应用真正的身份请看 `docker exec <容器> cat /proc/1/status` 里的 Uid。
ENTRYPOINT ["/usr/local/bin/api-entrypoint"]

# 命令写死：单 worker、固定端口，**不提供任何变量**。
#
# 单 worker 不是性能选择，是正确性约束：任务运行时表（`services/job_service._RUNTIMES`）、
# 任务总线（`services/job_bus`）、配置快照（`core/runtime_config`）、固定连接池
# （`core/engine`，`max_overflow=0`）、技能包缓存都是**进程内单例**。多一个 worker
# 就会出现「A 进程起的任务，B 进程的 SSE 永远等不到分片」。所以这里写 `--workers 1`
# 而不是省略它——省略等于把默认值当契约，而这台机器的默认值是可以被别人的参数盖掉的。
# 启动脚本另外拦一道 `--workers`（见 api-entrypoint.sh），把这个空间彻底关掉。
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
