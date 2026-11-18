# PRDwright 部署常用命令（简写，不承载任何逻辑）
#
# 适用：Linux / macOS / WSL（有 make 的环境）。
# Windows 原生 PowerShell 没有 make，直接敲 README §4 表格里的同名 docker 命令。
#
# 所有命令都在 deploy/ 目录下执行；compose 会读同目录的 .env。

# 要同时带上 nginx-proxy 覆盖文件时用：
#   make up COMPOSE_FILES="compose.yaml compose.proxy.yaml"
# 只带 compose.yaml 重建容器会丢掉 VIRTUAL_HOST 与 web-proxy 网络，域名会直接 SSL/502 报错。
COMPOSE_FILES ?= compose.yaml
COMPOSE ?= docker compose $(addprefix -f ,$(COMPOSE_FILES))
API_IMAGE ?= prdwright-api:local

.PHONY: help build up down logs ps restart shell volumes config keygen backup drill

help:
	@echo "make build    构建两个镜像"
	@echo "make keygen   生成主密钥（只需一次；可用 API_IMAGE=<tag> 覆盖镜像名）"
	@echo "make up       构建并后台起栈"
	@echo "make logs     跟踪 api 日志"
	@echo "make restart  重启 api（会触发排空）"
	@echo "make down     停栈（保留卷与数据）"
	@echo "make shell    进 api 容器"
	@echo "make volumes  列出本项目的命名卷"
	@echo "make config   打印展开后的完整编排"
	@echo "make backup   备份数据库（pg_dump + 自检 + sha256，产物落 backups/）"
	@echo "make drill    回滚预演练（本机库上备份→破坏→恢复，记耗时；产物落系统临时目录）"

build:
	$(COMPOSE) build

keygen:
	docker run --rm --user 10001:10001 \
	  -e PRDWRIGHT_MASTER_KEY_PATH=/secrets/master.key \
	  -v prdwright_secrets:/secrets \
	  --entrypoint python $(API_IMAGE) \
	  /app/scripts/keygen/generate_keys.py

up:
	$(COMPOSE) up -d --build

logs:
	$(COMPOSE) logs -f api

ps:
	$(COMPOSE) ps

restart:
	$(COMPOSE) restart api

down:
	$(COMPOSE) down

shell:
	$(COMPOSE) exec api sh

volumes:
	docker volume ls --filter name=prdwright

config:
	$(COMPOSE) config

# 备份：产出 backups/prdwright-<日期>.dump 与同名 .sha256（pg_dump -Fc + 自检）。
# 脚本本体跑在宿主的项目 uv 环境里；pg_dump 发生在一次性 postgres 容器里（走
# prdwright_net 连 db 服务），所以栈起着或停着都能备份。前置条件是 api 镜像已构建
# （make build 或 make up），以及本机有与服务端同主版本的 postgres 镜像。
backup:
	uv run --project ../backend python backup.py

# 回滚预演练：在**本机开发库**（默认 127.0.0.1:5433）上建一个一次性的
# prdwright_drill 库，真跑一遍「迁移 → 造数据 → pg_dump → 砸库 → pg_restore →
# 校验」，逐见证每步耗时。幂等，跑完删库删临时目录，不碰 deploy/backups/
# 也不碰 compose 的命名卷（生产卷）。完整说明见 README §6.7 与 ROLLBACK.md。
drill:
	uv run --project ../backend python drill.py
