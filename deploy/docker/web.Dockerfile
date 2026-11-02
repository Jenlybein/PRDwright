# PRDwright 静态站点镜像：用户端产物 + 管理端产物 + 反向代理
#
# 构建上下文是**仓库根**（要同时看见 frontend/ 与 frontend_admin/）：
#
#     docker build -f deploy/docker/web.Dockerfile -t prdwright-web .
#
# 根目录的 `.dockerignore` 负责把 node_modules / .venv / data / logs / dist / .git 挡在上下文外。
#
# 为什么前端自成镜像、不并进 api 镜像：`npm ci` 与 `uv sync` 的缓存键完全不同，
# 合在一个 Dockerfile 里会让「只改后端」也重装一遍 node_modules（6770 个文件）。
# 分开之后，两条构建链互不影响，镜像各管一件事。
#
# 最终镜像只含 nginx 运行时 + 两份 dist + 一份反代配置：没有源码、没有 node/npm、
# 没有 node_modules。看这个镜像就能回答「线上是哪个前端版本」。

# ============================================================================
# 阶段 1 —— 构建两个前端产物
# ============================================================================
FROM node:22-alpine AS build

WORKDIR /build

# 依赖层单独成层：package.json / lock 没变时，改源码不会重装依赖。
# 两个前端**完全独立**（各有自己的 lock），管理端必须带 --legacy-peer-deps，
# 与本地安装、ci.yml 的 frontend-admin job 保持同一套解析结果。
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci --no-audit --no-fund

COPY frontend_admin/package.json frontend_admin/package-lock.json ./frontend_admin/
RUN cd frontend_admin && npm ci --legacy-peer-deps --no-audit --no-fund

# 再拷源码。`.dockerignore` 已排除 **/node_modules，宿主机的依赖不会盖掉上一层的安装结果。
COPY frontend/ ./frontend/
COPY frontend_admin/ ./frontend_admin/

# 只跑 `vite build`，不跑 type-check：类型检查是 CI 的职责（ci.yml 两个前端 job 都跑
# `npm run build`，其中含 vue-tsc），镜像构建只负责回答「产物能不能出来」。
# 附带好处是 vue-tsc 不会在构建层里写出 *.tsbuildinfo（那会让缓存层变得不确定）。
RUN cd frontend && npm run build-only
RUN cd frontend_admin && npm run build-only

# ============================================================================
# 阶段 2 —— 只带产物的静态站点
# ============================================================================
FROM nginx:alpine AS site

# 站点配置：单入口三路分派（`/` 用户端、`/admin/` 管理端、`/api/` 反代）。
# 覆盖官方镜像自带的 default.conf，不额外新增 server。
COPY deploy/docker/nginx.conf /etc/nginx/conf.d/default.conf

# 用户端产物落站点根；管理端产物落站点根的 admin/ 子目录。
# 管理端的 vite `base` 是 `/admin/`，产物里的 JS/CSS 引用全部带这个前缀，
# 所以落地路径必须正好是 `/admin/`，否则每一个资源都会 404。
COPY --from=build /build/frontend/dist/ /usr/share/nginx/html/
COPY --from=build /build/frontend_admin/dist/ /usr/share/nginx/html/admin/

# 官方镜像自带的演示页与默认错误页不属于交付物
RUN rm -f /usr/share/nginx/html/index.nginx-debian.html /usr/share/nginx/html/50x.html

EXPOSE 80

# 只证明「静态文件服务 + 反代进程活着」。它**不**探 api：
# api 有它自己的就绪探针（检查库可写与技能包可加载），那边没好不该把站点反复重启。
# 这里用 busybox 的 wget，因为 nginx:alpine 不含 curl。
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

# 说明：nginx 官方镜像的 master 以 root 启动、worker 以 nginx 用户运行（要绑 80 端口就得如此）。
# 这个镜像里没有用户代码、没有可写卷、文件全部只读，root 的风险面仅限 nginx 自身。
# 若以后要把 master 也降权：换 nginxinc/nginx-unprivileged + 监听 8080，端口映射跟着改编排。
