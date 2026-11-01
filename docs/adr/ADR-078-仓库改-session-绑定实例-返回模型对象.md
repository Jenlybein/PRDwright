# ADR-078

> 源：v6 层（原 `docs/architecture/v6/ADR.md`，已并入本目录）｜ [索引](./README.md)

### 仓库改 session 绑定实例、返回模型对象

**状态**：已定

**背景**

仓库层今天是「静态函数命名空间」：86 个 `@staticmethod`，按 `XxxRepository.method(...)`
调用，71 个调用点在服务层与 API 层，且从没传过 session。

**决定**

1. 全部改为**实例方法**，构造参数是 `Session`：`Repo(session).method(...)`；
2. 仓库**不 commit / 不 rollback / 不 close**；
3. 返回 **ORM 模型对象**，不返回 `dict`；
4. 需要自增 id 时用 `flush()`，不用 `commit()` + `lastrowid`。

**理由**

第 1 条是被第 2 条逼出来的：仓库不持有连接，就必须从外部拿 session；而 `@staticmethod`
的签名里没有地方放它。加一个 `conn=` 关键字参数（T15.3 的做法）就是今天 `lease(conn)` 的
翻版——86 个函数多一个可选参数、71 个调用点各自决定传不传，事务边界继续散着。

第 3 条换来的收益是编译期：`job.stauts` 会被类型检查抓住，`job["stauts"]` 要等运行时，
而这类字段名错误在「返回 dict」的写法下只能靠测试覆盖到具体分支才暴露。

**代价（必须承认）**

- 71 个调用点要改；`job["status"]` → `job.status`；
- 有 2 条测试断言 `["is_admin"] == 1`，改模型后应为 `is True`（形态由 API 层保证不变）；
- 断言仓库返回 `dict` 的测试要改成断言模型字段。

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| 保持 `@staticmethod`，内部 `SessionLocal()` 自取 | 事务边界仍在仓库层，跨表原子依然做不到；且每个方法都要管 session 生命周期 |
| 加 `conn: Session | None = None` 参数（T15.3 路线） | 就是 `lease()` 的翻版，且这次要改 86 个签名 + 71 个调用点，成本不低、收益只有「少改调用点」 |
| 返回 `dict`（模型在仓库内转掉） | 放弃类型检查收益；且转换层要做 `__dict__` 过滤，容易漏 `metadata` 这类改名映射 |
| 返回独立 DTO（Pydantic） | 多一层模型定义，与 SQLModel 的「一个模型两用」初衷冲突 |

**后果**

- `to_history_item` / `to_display_turn` 这类投影函数保留，入参从 `dict` 改为模型对象；
- 响应组装统一走 `Model.model_validate(obj, from_attributes=True)`；
- `expire_on_commit=False` 是必须的，否则事务外读属性会 `DetachedInstanceError`（见 ADR-079）。

---
