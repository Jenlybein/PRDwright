# ADR-044：运行时配置用「DB 只存覆盖项」+「一次生成冻结一份快照」

> 源：v4 层（原 `docs/architecture/v4/ADR.md`，已并入本目录）｜ [索引](./README.md)

### 背景

v4 之前全部配置都在 `.env`：改一个值要改文件、重启服务。后台因此无法调参数，
「换模型供应商」更是要改代码里的 `Literal` 与分支。

难点不在「读出来的值能变」，而在两件事：

1. 配置的读取点有**三种形态**：每次调用读（`llm_factory`）、模块级快照（`main.py`）、
   **实例级快照**（`PrdSectionWriter.__init__` 拷了 5 个值、三处 LLM 实例缓存）。
   只清 `lru_cache` 覆盖不到后两种——表现是「改了配置，接口返回新值、生成还用旧值」。
2. 反过来，如果每个读取点都即时读，一次 20 分钟的生成跑到一半就会**参数漂移**。

### 决定

**一、`system_config` 只存覆盖项。** 没有某一行 = 用 env 或代码默认值。
所以「升级后行为零变化」是**结构保证**，而不是靠人记得别写默认值；
「恢复默认」就是删掉那一行。白名单是**硬编码常量**（不入表）：能通过配置扩展的
白名单等于没有白名单，而它是安全边界（`db_path` 一旦可改，配置层就与 `connect()`
形成递归）。

**二、一次生成冻结一份快照。** `dispatch` 取快照存进 `JobRuntime`，
`run_job` 用 `use_config` 挂到 ContextVar——与 `use_meter` 是同一套机制，学一次用两处。
直连 SSE 的路由（`/api/conversation/*-stream`）在 `make_sse_generator` 里统一冻结，
因为**事件流是在响应返回之后才被消费的**，放在路由函数体里的 `with` 早就退出了。

**三、构造期快照全部拆除。** `PrdSectionWriter` 的 5 个属性改成只读属性（12 处
使用点一行不用改），三处 LLM 缓存改按**同一个配置指纹**重建（`config_fingerprint()`）。
指纹集中在一处，否则会出现「撰写实例换了、审核实例还是旧的」——同一次生成里两个
实例参数不一致，正文与批注的口径就对不上。

**四、供应商按协议建模，不按厂商。** `protocol` 六值（`openai_compatible` /
`anthropic` / `ark` / `google_genai` / `nvidia` / `huggingface`）。已查证
`langchain-deepseek` 等包都是 `BaseChatOpenAI` 的薄封装（只把 `base_url` 写死），
所以接一家新厂商只需填 `base_url`，**不改代码、不加依赖**。

**五、所有适配器收敛进 `llm_factory.py`**（`ark_llm.py` 并入后删除）。
分层守卫的白名单从两个文件缩成一个，规则变成一句话：只有工厂能碰供应商 SDK。

**六、密钥只存变量名**（ADR-042 不变）。新增 `core/secrets.py` 按键名查找——
因为 `pydantic-settings` 读 `.env` 但**不写 `os.environ`**，运行时新增的供应商
按键名去 `os.environ` 找会永远为空，界面一直提示「未配置」。

### Consequences

**变容易**

- 后台改完即生效，不用重启；多 worker 靠 5 秒 TTL 收敛。
- 加一家 OpenAI 兼容的厂商是**数据操作**，不是代码改动。
- 排障时页面直接显示「改前 / 改后 / 是否真的生效」。

**变难 / 代价**

- 多了一层抽象：读配置要经 `get_runtime_config().settings`，而不是 `get_settings()`。
  好处是取值写法完全不变（`Settings` 的派生属性原样可用）。
- `llm_factory.py` 从 148 行长到约 550 行，同时装着工厂与一个 `BaseChatModel` 子类。
- 三个新协议依赖偏重（`protobuf` / `tokenizers`），装完必须跑全量确认没带崩别的包。

**验证**

- `test_runtime_config.py`（22 条）：默认行为零变化、白名单、降级不抛、不递归。
- `test_config_effect.py`（9 条）：死配置修复、指纹重建、快照拆除、任务冻结。
- `test_admin_config_api.py`（30 条）：门禁逐条、密钥不外泄、规则只在服务层。
- 端到端：改并发度 → 保存 → `revision` 变化 → 复读为 `source=db`。

---
