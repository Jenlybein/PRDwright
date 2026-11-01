# ADR-029：正文全搬进 `document_versions`，`documents` 只留「当前版本」指针

> 源：v2 层（原 `docs/architecture/v2/ADR.md`，已并入本目录）｜ [索引](./README.md)

**状态**：Accepted（2026-10-16 裁决并落地）

> **补记（同日）：删除历史版本**。第 2 条写的是「版本**何时产生**」，没写「版本**何时消失**」——
> 补上：**只有历史版本可以删除，当前版本不给删**（后端 `INVALID_REQUEST` / 400，`detail` 里说明
> 原因）。想换当前用的那一版请走 `restore`（**追加**一条新版本）；用「删掉当前版本」来换版本，
> 等于把这份文档的正文直接抽走，还要额外规定「删完当前指向谁」——两件事性质不同，不该由同一个
> 动作承担。这条限制**写在仓储的 SQL 里**（`WHERE ... AND v.id != d.current_version_id`）而不是
> 「先查一次、再决定删不删」：两次查询之间若有别的写入把它变成当前版本，先查后删就会放行，
> 而那时删掉的是文档正在用的正文。
> 另外，删除**只重拉版本列表、不重拉当前文档**——正文一点没变，多拉一次只会平白闪一下编辑器。
> 本文正文保留裁决当时的形态，不回改。

### Context

ADR-021 把正文从「前端快照里的三个字符串」搬进后端 `documents` 单表，解决了**一个内容两个真源**与**写入口分散**两件事。但那一步只做到「谁是主人」，没做到「改坏了能回头」：一次生成、一次 AI 优化都**直接覆盖**同一行的 `content`，覆盖之后就只剩当前这一份——想拿回上一版，唯一的办法是凭记忆重写。

ADR-021 与 v3 的 `ARCHITECTURE.md` 4.3 都把版本链标为「本期明确推迟」，并明确写了结构位置怎么留：**往上叠一张 `document_versions`，把 `documents.content` 换成指向 current 版本**，不推翻今天任何一条约束。触发这一版的，正是 ADR-021 自己把这两个条件凑齐了：

1. **文档已经归后端所有**，因此「这份文档一共几版」是一个后端答得清的问题（在此之前它散在前端内存里，刷新即无）；
2. **每次改写都留下了清单与回执**（ADR-023），因此「哪一版被谁改过、花在哪儿」有据可查。

今天连「改了几次」都还没有地方记。而 `optimize` 这种「对着文档反复迭代」的用法恰恰最需要回退——用户满意的那一版，很可能不是最后一版。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| A. 继续不做（维持单表覆盖） | 零改动 | 改坏了回不去；AI 优化迭代几轮之后，之前满意的那一版永久丢失 |
| **B. 正文全搬进 `document_versions`，`documents` 只留 `current_version_id` 指针（推荐）** | 版本链成为正文的**唯一**家，「当前是哪一版」由指针显式表达；历史只增不改 | 单表读正文的代码全要改成 JOIN；删掉原表正文列是**单向门**，回滚只能靠备份 |
| C. 版本表只存「被替换掉的旧稿」，当前稿仍留在 `documents.content` | 读当前稿的代码零改动，改动面最小 | 正文有两个家（当前稿一列 + 历史一表），列版本列表要跨两处拼；且「当前稿」与「历史里最新那条」谁是权威只能靠约定维持，迟早漂移 |

### Decision

**选 B。**

1. **新增 `document_versions`**：`document_id / version_no / content / content_sha / pending_items / last_run_summary / source / note / metadata / created_at / updated_at`，`UNIQUE(document_id, version_no)`；`document_id` 外键 `ON DELETE CASCADE`。`documents` 退化为「**身份 + 当前版本指针**」（`current_version_id`，`ON DELETE SET NULL`）——正文只有一个家。
2. **版本产生时机**（这次裁决的核心口径，也是最容易写错的一处）：
   - **整篇生成**（`prd` / `api_docs` / `prompts`）→ **追加一条新版本**，并让下游产物失效；
   - **`optimize` / `clarify`**（对同一份文档的就地改写）与**用户手改** → **就地改当前版本**，**不新增条目**；
   - **「存为新版本」(checkpoint) 与「恢复」(restore)** → 追加新版本。
   前两条由 `job_service._regenerates_whole_document` 判定，且它同时驱动「是否追加版本」与「是否删下游」——两个语义**恒等**，因此收敛成同一个 `append_version` 标志，避免出现「该追加却不删下游」这种自相矛盾的组合。
3. **恢复的实现是「读源版本 → 追加一条 `source='restore'` 的新版本」**，**不把指针拨回旧版本**。拨指针会丢掉「曾经恢复到哪一版」的痕迹，更要紧的是：旧版本一旦被就地改（手改 / 优化），历史就被篡改了——「只增不改」是版本链唯一站得住的性质。
4. **待确认清单与用量回执随版本存档**：`pending_items` / `last_run_summary` 是版本行上的**显式列**（不塞进 `metadata` 口袋）——它们是「一次生成」的第一类产物（ADR-023），恢复某一版时连清单一起还原，而不是恢复出一份与正文对不上的旧清单。
5. **对外结构逐字段不变**：`DocumentItem`（`kind` / `content` / `pending_items` / `last_run_summary` / `updated_at`）照旧，因此**读文档的调用方（路由、前端 store）一行都不用改**；版本是一组**新增**的读 / 写能力，不是替换。`_to_item` 仍从「文档行 + 当前版本行」的 JOIN 结果上取同样的几个字段。
6. **迁移拆三步**（沿用 ADR-022 的清单纪律）：v4 建版本表 + 给 `documents` 加指针；v5 逐行容错地把现有文档回填成 `version_no=1, source='migrated'`；v6 删掉 `documents` 上的 `content / content_sha / pending_items / last_run_summary`。**v6 是单向门**，因此单独成步、并把备份写进部署注意事项。
7. **删除用原生 `ALTER TABLE ... DROP COLUMN`，不重建表**。理由不是省事：`PRAGMA foreign_keys` **在事务内是空操作**，而迁移运行器把每一步包在 `BEGIN ... COMMIT` 里（ADR-022），所以「关外键 → 重建表 → 拷数据 → 改名」那套在步骤内部根本关不掉外键。四列都是普通列（非主键、无 UNIQUE、未进索引 / 外键 / CHECK / 触发器 / 视图），原生删列直接可用。
8. **归属仍由 `solutions` 决定**（`document_versions → documents → solutions` 两级 JOIN），因此版本路由挂在 **solution 作用域**下：`/api/solutions/{id}/documents/{kind}/versions` 等，而不是 4.3 草稿里的 `/api/documents/{kind}/versions`——后者没法在 SQL 层过滤归属，会破坏「越权与不存在不可区分」的既有口径（ADR-009）。版本不存在复用 `SOLUTION_NOT_FOUND`（404），错误码表不膨胀（ADR-012）。

### Consequences

- **变容易**：「这份文档一共几版、每版是怎么来的、什么时候」一句 SQL 就问得出来；改坏了能回到任意一版；`metadata` 口袋为 v3.1 E 的 `quality_gate` 留好了位置，不必再动结构。
- **变困难**：删列是**单向门**——v6 之后旧代码读不到 `documents.content`，回滚只能恢复迁移前的库文件；`documents` ↔ `document_versions` 是一对**环形外键**（指针 SET NULL / 外键 CASCADE），写入顺序必须是「**先空指针插文档行 → 插版本 → 回填指针**」，反过来会撞上「指针指向还不存在的版本」，而删方案要把两级都清干净（由测试钉死）；读当前文档的查询一律变成 JOIN，「当前版本」这件事不再由数据摆放隐含表达，而是由指针显式给出。
- **中立**：ADR-009 没有被破坏——版本表是后端**自己的**表，`solutions.data` 仍是不透明快照。ADR-021 的三条（单表所有权、写入口唯一、前端两条通道）**全部成立**，这次只是把「正文」从那张表里挪进它自己的历史里；`commit_generated` 的调用方从「整批替换三列」变成「追加一版 / 就地改一版」，语义没变，落点没变。

---
