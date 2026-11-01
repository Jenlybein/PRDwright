# 库结构勘察：prdwright.db

- 表 14 张，共 2001 行，文件 1788 KB
- 只读打开（`mode=ro`），不存在任何写入路径

## 1 表的规模

| 表 | 行数 |
| --- | ---: |
| `admin_audit_log` | 5 |
| `app_logs` | 1905 |
| `auth_sessions` | 1 |
| `chat_sessions` | 3 |
| `chat_turns` | 9 |
| `document_versions` | 8 |
| `documents` | 7 |
| `generation_jobs` | 54 |
| `invite_codes` | 1 |
| `login_failures` | 0 |
| `model_providers` | 1 |
| `solutions` | 4 |
| `system_config` | 2 |
| `users` | 1 |

## 2 文本列的长度分布

`超字符阈值` 只列出**有命中**的档位。`索引` 一列是「该列是否需要能建索引」——MySQL 下 `TEXT` 列建索引必须给前缀长度，主键更是完全不行。

`max字节` 与 `超64KB` 才是 `TEXT` 够不够的依据：MySQL 的 `TEXT` 上限 64KB 按**字节**算，中文一字 3 字节——11500 字的中文 PRD 已经是 34KB，不是「才 11K」。

| 表.列 | 行 | max字 | max字节 | p99 | NULL | 超字符阈值 | 超64KB | 索引 | 建议 |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |
| `admin_audit_log.actor_name` | 5 | 6 | 6 | 6 | 0 | — | — | — | 保留 TEXT |
| `admin_audit_log.action` | 5 | 15 | 15 | 15 | 0 | — | — | — | 保留 TEXT |
| `admin_audit_log.target` | 5 | 14 | 14 | 14 | 0 | — | — | — | 保留 TEXT |
| `admin_audit_log.detail` | 5 | 75 | 85 | 75 | 0 | >64: 2 | — | — | 保留 TEXT |
| `admin_audit_log.request_id` | 5 | 32 | 32 | 32 | 0 | — | — | — | 保留 TEXT |
| `admin_audit_log.ts` | 5 | 26 | 26 | 26 | 0 | — | — | idx_admin_audit_actor、idx_admin_audit_ts | `VARCHAR(32)` |
| `app_logs.ts` | 1905 | 30 | 30 | 24 | 0 | — | — | idx_app_logs_level、idx_app_logs_ts | `VARCHAR(32)` |
| `app_logs.level` | 1905 | 7 | 7 | 4 | 0 | — | — | idx_app_logs_level | `VARCHAR(32)` |
| `app_logs.logger` | 1905 | 37 | 37 | 28 | 0 | — | — | — | 保留 TEXT |
| `app_logs.msg` | 1905 | 94 | 94 | 16 | 0 | >64: 16 | — | — | 保留 TEXT |
| `app_logs.request_id` | 1905 | 32 | 32 | 32 | 769 | — | — | idx_app_logs_request | `VARCHAR(32)` |
| `app_logs.event` | 1905 | 20 | 20 | 17 | 28 | — | — | — | 保留 TEXT |
| `app_logs.extra_json` | 1905 | 191 | 207 | 191 | 0 | >64: 1866、>128: 758 | — | — | 保留 TEXT |
| `app_logs.exc` | 1905 | 1084 | 1184 | 1084 | 1902 | >64: 3、>128: 3、>255: 3、>512: 1、>1024: 1 | — | — | 保留 TEXT |
| `auth_sessions.token_hash` | 1 | 64 | 64 | 64 | 0 | — | — | sqlite_autoindex_auth_sessions_1 | `VARCHAR(64)` |
| `auth_sessions.created_at` | 1 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `auth_sessions.expires_at` | 1 | 26 | 26 | 26 | 0 | — | — | idx_auth_sessions_expires | `VARCHAR(32)` |
| `chat_sessions.stage` | 3 | 11 | 11 | 11 | 0 | — | — | ux_chat_sessions_thread | `VARCHAR(32)` |
| `chat_sessions.conversation_id` | 3 | 12 | 12 | 12 | 0 | — | — | — | 保留 TEXT |
| `chat_sessions.updated_at` | 3 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `chat_turns.stage` | 9 | 11 | 11 | 11 | 0 | — | — | idx_chat_turns_thread | `VARCHAR(32)` |
| `chat_turns.role` | 9 | 9 | 9 | 9 | 0 | — | — | ux_chat_turns_job_role | `VARCHAR(32)` |
| `chat_turns.content` | 9 | 1359 | 3425 | 1359 | 0 | >64: 8、>128: 7、>255: 7、>512: 4、>1024: 4 | — | — | 保留 TEXT |
| `chat_turns.tool_call_id` | 9 | 29 | 29 | 29 | 0 | — | — | — | 保留 TEXT |
| `chat_turns.tool_result` | 9 | 160 | 208 | 160 | 0 | >64: 1、>128: 1 | — | — | 保留 TEXT |
| `chat_turns.asks` | 9 | 591 | 927 | 591 | 0 | >64: 4、>128: 4、>255: 4、>512: 2 | — | — | 保留 TEXT |
| `chat_turns.answers` | 9 | 98 | 146 | 98 | 0 | >64: 1 | — | — | 保留 TEXT |
| `chat_turns.tool_calls` | 9 | 597 | 914 | 597 | 0 | >64: 4、>128: 4、>255: 4、>512: 2 | — | — | 保留 TEXT |
| `chat_turns.created_at` | 9 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `document_versions.content` | 8 | 11566 | 24534 | 11566 | 0 | >64: 8、>128: 8、>255: 8、>512: 8、>1024: 8、>4096: 3 | — | — | 保留 TEXT |
| `document_versions.content_sha` | 8 | 12 | 12 | 12 | 0 | — | — | — | 保留 TEXT |
| `document_versions.pending_items` | 8 | 3724 | 5276 | 3724 | 0 | >64: 6、>128: 6、>255: 6、>512: 6、>1024: 6 | — | — | 保留 TEXT |
| `document_versions.last_run_summary` | 8 | 839 | 839 | 839 | 0 | >64: 8、>128: 8、>255: 8、>512: 8 | — | — | 保留 TEXT |
| `document_versions.source` | 8 | 8 | 8 | 8 | 0 | — | — | — | 保留 TEXT |
| `document_versions.note` | 8 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `document_versions.metadata` | 8 | 75 | 75 | 75 | 0 | >64: 3 | — | — | 保留 TEXT |
| `document_versions.created_at` | 8 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `document_versions.updated_at` | 8 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `documents.kind` | 7 | 8 | 8 | 8 | 0 | — | — | sqlite_autoindex_documents_1 | `VARCHAR(32)` |
| `documents.created_at` | 7 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `documents.updated_at` | 7 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.kind` | 54 | 12 | 12 | 12 | 0 | — | — | idx_generation_jobs_recover | `VARCHAR(32)` |
| `generation_jobs.status` | 54 | 9 | 9 | 9 | 0 | — | — | idx_generation_jobs_recover | `VARCHAR(32)` |
| `generation_jobs.phase` | 54 | 9 | 9 | 9 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.draft_content` | 54 | 26957 | 36919 | 26957 | 0 | >64: 53、>128: 52、>255: 52、>512: 38、>1024: 36、>4096: 20、>16384: 4 | — | — | 保留 TEXT |
| `generation_jobs.completed_sections` | 54 | 7125 | 15777 | 7125 | 0 | >64: 13、>128: 13、>255: 13、>512: 13、>1024: 13、>4096: 4 | — | — | 保留 TEXT |
| `generation_jobs.input_fingerprint` | 54 | 64 | 64 | 64 | 41 | — | — | — | 保留 TEXT |
| `generation_jobs.usage` | 54 | 843 | 843 | 843 | 2 | >64: 52、>128: 52、>255: 52、>512: 31 | — | — | 保留 TEXT |
| `generation_jobs.error_code` | 54 | 18 | 18 | 18 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.error_message` | 54 | 55 | 165 | 55 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.created_at` | 54 | 26 | 26 | 26 | 0 | — | — | idx_generation_jobs_owner | `VARCHAR(32)` |
| `generation_jobs.updated_at` | 54 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.pending_items` | 54 | 3743 | 5351 | 3743 | 0 | >64: 23、>128: 23、>255: 23、>512: 23、>1024: 18 | — | — | 保留 TEXT |
| `generation_jobs.doc_kind` | 54 | 8 | 8 | 8 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.chat_stage` | 54 | 11 | 11 | 11 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.provider_protocol` | 54 | 3 | 3 | 3 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.provider_config_id` | 54 | 7 | 7 | 7 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.provider_name` | 54 | 4 | 12 | 4 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.provider_model` | 54 | 28 | 28 | 28 | 0 | — | — | — | 保留 TEXT |
| `invite_codes.code` | 1 | 6 | 6 | 6 | 0 | — | — | sqlite_autoindex_invite_codes_1 | `VARCHAR(32)` |
| `invite_codes.expires_at` | 1 | 0 | 0 | 0 | 1 | — | — | — | 保留 TEXT |
| `invite_codes.created_at` | 1 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `invite_codes.note` | 1 | 25 | 49 | 25 | 0 | — | — | — | 保留 TEXT |
| `model_providers.id` | 1 | 14 | 14 | 14 | 0 | — | — | sqlite_autoindex_model_providers_1 | `VARCHAR(32)` |
| `model_providers.name` | 1 | 4 | 12 | 4 | 0 | — | — | — | 保留 TEXT |
| `model_providers.protocol` | 1 | 3 | 3 | 3 | 0 | — | — | — | 保留 TEXT |
| `model_providers.base_url` | 1 | 40 | 40 | 40 | 0 | — | — | — | 保留 TEXT |
| `model_providers.default_model` | 1 | 28 | 28 | 28 | 0 | — | — | — | 保留 TEXT |
| `model_providers.extra_json` | 1 | 2 | 2 | 2 | 0 | — | — | — | 保留 TEXT |
| `model_providers.created_at` | 1 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `model_providers.updated_at` | 1 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `model_providers.value_token` | 1 | 140 | 140 | 140 | 0 | >64: 1、>128: 1 | — | — | 保留 TEXT |
| `solutions.title` | 4 | 9 | 9 | 9 | 0 | — | — | — | 保留 TEXT |
| `solutions.mode` | 4 | 10 | 10 | 10 | 0 | — | — | — | 保留 TEXT |
| `solutions.stage` | 4 | 15 | 15 | 15 | 0 | — | — | — | 保留 TEXT |
| `solutions.data` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.created_at` | 4 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `solutions.updated_at` | 4 | 26 | 26 | 26 | 0 | — | — | idx_solutions_owner | `VARCHAR(32)` |
| `solutions.content_updated_at` | 4 | 26 | 26 | 26 | 0 | — | — | idx_solutions_owner_content | `VARCHAR(32)` |
| `solutions.view_state` | 4 | 15 | 15 | 15 | 0 | — | — | — | 保留 TEXT |
| `solutions.entry_mode` | 4 | 10 | 10 | 10 | 0 | — | — | — | 保留 TEXT |
| `solutions.chat_tab` | 4 | 11 | 11 | 11 | 0 | — | — | — | 保留 TEXT |
| `solutions.summary_json` | 4 | 1166 | 1800 | 1166 | 0 | >64: 4、>128: 4、>255: 4、>512: 4、>1024: 4 | — | — | 保留 TEXT |
| `solutions.quick_product_name` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.quick_requirements` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.draft_prd_text` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.paste_prd_text` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.paste_api_docs_text` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.active_job_kind` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.active_job_doc_kind` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `system_config.key` | 2 | 24 | 24 | 24 | 0 | — | — | sqlite_autoindex_system_config_1 | `VARCHAR(32)` |
| `system_config.value` | 2 | 16 | 16 | 16 | 0 | — | — | — | 保留 TEXT |
| `system_config.updated_at` | 2 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `users.username` | 1 | 6 | 6 | 6 | 0 | — | — | sqlite_autoindex_users_1 | `VARCHAR(32)` |
| `users.password_hash` | 1 | 128 | 128 | 128 | 0 | >64: 1 | — | — | 保留 TEXT |
| `users.created_at` | 1 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |

## 3 唯一约束的重复检查

SQLite 这边约束**已经生效**（`PRAGMA foreign_keys=ON`），所以正常情况下不该有重复。真正要检查的是 **`fold` 一列**：换库后 MySQL 默认 collation 不区分大小写，`Admin` 与 `admin` 会变成同一个键。

| 表 | 约束 | 列 | 精确重复 | 忽略大小写重复 |
| --- | --- | --- | ---: | ---: |
| `auth_sessions` | sqlite_autoindex_auth_sessions_1 | token_hash | 0 | 同左 |
| `chat_sessions` | ux_chat_sessions_thread | solution_id, stage | 0 | 同左 |
| `chat_turns` | ux_chat_turns_job_role | job_id, role | 0 | 同左 |
| `document_versions` | <unique-constraint> | document_id, version_no | 0 | 同左 |
| `documents` | <unique-constraint> | solution_id, kind | 0 | 同左 |
| `invite_codes` | sqlite_autoindex_invite_codes_1 | code | 0 | 同左 |
| `model_providers` | sqlite_autoindex_model_providers_1 | id | 0 | 同左 |
| `system_config` | sqlite_autoindex_system_config_1 | key | 0 | 同左 |
| `users` | <unique-constraint> | username | 0 | 同左 |

## 4 布尔列的取值合规性

按 `models.BOOLEAN_COLUMNS` 白名单检查，**不按数据分布猜**——`model_providers.sort_order` 在全表恰好只有 0 的时候也满足「只落在 0/1」。这张表回答的是：换库后 `BOOLEAN` ↔ `TINYINT(1)` 的映射会不会丢掉非法值。

| 表.列 | 实测取值 | 合规 |
| --- | --- | --- |
| `chat_turns.interrupted` | 0 | 是 |
| `model_providers.thinking_enabled` | 0 | 是 |
| `model_providers.builtin` | 0 | 是 |
| `model_providers.enabled` | 1 | 是 |
| `users.is_active` | 1 | 是 |
| `users.is_admin` | 1 | 是 |

白名单内所有布尔列的取值都在 `{0, 1, NULL}` 内。

## 5 时间列的格式一致性

`UTC(Z)` 是 `utc_now_text()` 的规范形态。`ISO` 但**不带** Z 的行需要盯——那可能混进了本地时间，换库改成 `DATETIME` 时会错 8 小时。

| 表.列 | 行 | NULL | ISO 形态 | 带 Z | 非 Z | 脏格式 | 格式异常 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `admin_audit_log.ts` | 5 | 0 | 5 | 0 | 5 | — | — |
| `app_logs.ts` | 1905 | 0 | 1905 | 1905 | — | 16 | — |
| `auth_sessions.created_at` | 1 | 0 | 1 | 0 | 1 | — | — |
| `auth_sessions.expires_at` | 1 | 0 | 1 | 0 | 1 | — | — |
| `chat_sessions.updated_at` | 3 | 0 | 3 | 0 | 3 | — | — |
| `chat_turns.created_at` | 9 | 0 | 9 | 0 | 9 | — | — |
| `document_versions.created_at` | 8 | 0 | 8 | 0 | 8 | — | — |
| `document_versions.updated_at` | 8 | 0 | 8 | 0 | 8 | — | — |
| `documents.created_at` | 7 | 0 | 7 | 0 | 7 | — | — |
| `documents.updated_at` | 7 | 0 | 7 | 0 | 7 | — | — |
| `generation_jobs.created_at` | 54 | 0 | 54 | 0 | 54 | — | — |
| `generation_jobs.updated_at` | 54 | 0 | 54 | 0 | 54 | — | — |
| `invite_codes.expires_at` | 1 | 1 | 0 | 0 | — | — | — |
| `invite_codes.created_at` | 1 | 0 | 1 | 0 | 1 | — | — |
| `model_providers.created_at` | 1 | 0 | 1 | 0 | 1 | — | — |
| `model_providers.updated_at` | 1 | 0 | 1 | 0 | 1 | — | — |
| `solutions.created_at` | 4 | 0 | 4 | 0 | 4 | — | — |
| `solutions.updated_at` | 4 | 0 | 4 | 0 | 4 | — | — |
| `solutions.content_updated_at` | 4 | 0 | 4 | 0 | 4 | — | — |
| `system_config.updated_at` | 2 | 0 | 2 | 0 | 2 | — | — |
| `users.created_at` | 1 | 0 | 1 | 0 | 1 | — | — |

所有时间列都形如 `YYYY-MM-DDTHH:MM:SS…`，可安全转 `DATETIME`。

**脏格式（`+00:00Z`）**：`app_logs.ts`（16 行）——`DATETIME` 解析会直接失败，必须在迁移脚本里剥掉偏移再补 `Z`。

## 6 由数据推出的候选长度

| 表.列 | 实测 max字 | 实测 max字节 | 建议档位 |
| --- | ---: | ---: | --- |
| `admin_audit_log.ts` | 26 | 26 | `VARCHAR(32)` |
| `app_logs.ts` | 30 | 30 | `VARCHAR(32)` |
| `app_logs.level` | 7 | 7 | `VARCHAR(32)` |
| `app_logs.request_id` | 32 | 32 | `VARCHAR(32)` |
| `auth_sessions.token_hash` | 64 | 64 | `VARCHAR(64)` |
| `auth_sessions.expires_at` | 26 | 26 | `VARCHAR(32)` |
| `chat_sessions.stage` | 11 | 11 | `VARCHAR(32)` |
| `chat_turns.stage` | 11 | 11 | `VARCHAR(32)` |
| `chat_turns.role` | 9 | 9 | `VARCHAR(32)` |
| `documents.kind` | 8 | 8 | `VARCHAR(32)` |
| `generation_jobs.kind` | 12 | 12 | `VARCHAR(32)` |
| `generation_jobs.status` | 9 | 9 | `VARCHAR(32)` |
| `generation_jobs.created_at` | 26 | 26 | `VARCHAR(32)` |
| `invite_codes.code` | 6 | 6 | `VARCHAR(32)` |
| `model_providers.id` | 14 | 14 | `VARCHAR(32)` |
| `solutions.updated_at` | 26 | 26 | `VARCHAR(32)` |
| `solutions.content_updated_at` | 26 | 26 | `VARCHAR(32)` |
| `system_config.key` | 24 | 24 | `VARCHAR(32)` |
| `users.username` | 6 | 6 | `VARCHAR(32)` |

> 这些档位只反映**当前这个库**。生产库必须用同一份脚本再跑一次——
> `VARCHAR(n)` 的 n 小于线上最大值时，迁移当天会以 `Data too long` 失败。

## 7 超过 16KB 的未索引文本列

这些列不建索引，所以不受 `VARCHAR` 约束；但 MySQL 的 `TEXT` 只有 64KB（字节），超过 16KB 就建议直接声明成 `MEDIUMTEXT`，别赌「还能再长一点」。

| 表.列 | max字 | max字节 | 建议 |
| --- | ---: | ---: | --- |
| `document_versions.content` | 11566 | 24534 | `MEDIUMTEXT` |
| `generation_jobs.draft_content` | 26957 | 36919 | `MEDIUMTEXT` |

