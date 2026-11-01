# ADR-051：模型接入只住在后台面板，`.env` 只留密钥值

> 源：v4 层（原 `docs/architecture/v4/ADR.md`，已并入本目录）｜ [索引](./README.md)

### Status

Accepted（2026-10-22）

### Context

ADR-050 把「一家供应商一个固定空位」改成了「配置条目」，v19 也清掉了六条播种行。
但**还有一份旧的配置源没撤**：`.env` 里仍然写着

```
DEFAULT_LLM_PROVIDER=ark
DEFAULT_LLM_MODEL=doubao-seed-2-1-turbo-260628
ARK_BASE_URL=… / ARK_MAX_TOKENS=… / ARK_THINKING_ENABLED=…
ANTHROPIC_API_URL=… / ANTHROPIC_MAX_TOKENS=… / OPENAI_…
```

而 `runtime_config._synthesize_from_env` 会在「表不可用 / 一条配置都没有」时
拿这些值合成一个 spec（id 形如 `env:ark`）继续跑。当时它是「升级后行为零变化」的
保险丝，现在变成了四个具体问题：

1. **两处真源。** 后台改了一条配置、env 里那份还在。出问题时要同时翻两个地方，
   而且没有任何提示告诉你去翻哪一个。
2. **界面与实际不一致。** 列表空着却能正常生成——用的是界面上不存在、也改不了的
   那条。管理员看到「还没有任何配置」却收到成功的回执，第一反应是「这个页面是不是假的」。
3. **回执里出现 `env:ark`。** 用户和运维都不知道那是什么，而账单需要它说得清。
4. **这套 env 本来就容易写错还不报错。** `ARK_API_BASE_URL` 被 `extra="ignore"`
   静默忽略、自定义地址从来没生效过——这个坑修过一次，但「键名对不上就没效果」
   的结构性问题还在。

另一个诱因是表单的两次反馈：**选了供应商仍要手打 `ARK_API_KEY`**、
**协议与密钥变量名这两个不可改的东西还摆在表单里**。它们的共同根因是
「同一件事有两个配置入口」——只要 env 还能配，后台就不是唯一入口。

### Decision

**配置进库，凭据留环境。**

1. **`Settings` 删除 10 个字段**：`default_llm_provider`、`default_llm_model`、
   `anthropic_api_url`、`openai_api_url`、`ark_base_url`、`ark_thinking_enabled`、
   `ark_max_tokens`、`anthropic_max_tokens`、`openai_max_tokens`，
   以及 `llm_context_window`（全局窗口覆盖）。
   连同一并删掉 `api_key_env_var()` / `require_api_key()`（它们的前提是「进程里只有一个供应商」）。
   **写进 `.env` 也不会有效果**——不是「不推荐」，是结构上没有地方读它。

   `llm_context_window` 一并删是因为它会**静默赢过**配置条目：
   原先是 `settings.llm_context_window or spec.context_window`，后台给某条填了 128k、
   env 里留着 32k，预算就按 32k 判——症状是「换了大窗口还老报超顶」。
   窗口现在只有一条链：**配置条目填的值 → `model_registry`**。
2. **删 `_synthesize_from_env` 与 `ENV_SPEC_PREFIX`。** `_resolve_active_provider`
   只剩两条规则：指针有效就用它；否则按 `sort_order` 取**第一条启用中**的。
   一条都没有时返回 `None`——这是正确答案，不是错误。
3. **第一条配置自动成为「使用中」。** 列表空着时它是唯一可选项，让用户再点一次
   「设为使用中」只是仪式；而忘了点就会得到「配好了却生成说没有配置」。
   已有指针时**不抢**：新建第二条不该把正在跑的那条顶掉。
4. **调用方直接说人话。** `get_llm()` 抛
   「还没有任何模型配置。请先在管理后台「模型配置」页新增一条……」；
   `/health` 的 llm 项同理；启动日志印「（尚无模型配置）」。这类文案本身就是修复指引，
   含糊一句「没有可用的模型供应商」会让人去翻 `.env`——而答案恰好是「`.env` 里没有了」。
5. **v16 迁移改成常量播种**（不再读 `Settings`）。老库不会重跑 v16，新库上它插下的
   六行随即被 v19 删除，所以改成常量对最终状态没有影响；不这么改，删字段就会让
   全新库的迁移直接 `AttributeError`。
6. **密钥值仍在环境里**（ADR-042 不变）。数据库只存**变量名**，按名取值的入口是
   `core/secrets.lookup_env`（不经过 `Settings`）。配置可以进库，凭据不行——
   这是两件事，不能因为「都迁到后台」就合并。
7. **`.env.example` 只留三把密钥**，并写明「供应商 / 协议 / 地址 / 型号 / 上限
   都不在这里配置」。
8. **空状态只留一个新增入口。** 右上角已有「+ 新增配置」，正文里再摆一个
   「新增第一条配置」会让人先愣一下「这两个是不是不一样」。

### 本期不做

| 本期不做 | 理由 |
| --- | --- |
| 密钥托管（把值也迁进后台） | 要先解决加密存储、轮换与泄露后处置，与「配置住哪儿」正交（ADR-042 仍有效） |
| 把 `token_budget.evaluate(provider=...)` 改名 `protocol` | 值域同时接受厂商名与协议名，改名反而不准；已在 docstring 写清并加守门用例 |
| 后台展示「旧 `.env` 里还残留哪些键」 | 这些键已经没有任何读取点，提示一个「写了但没用」的清单只会制造噪音 |

### Consequences

- **变容易**：只有一个地方配模型；列表与实际生效的东西永远一致；
  排查第一句话是「去后台看一眼」，不必再问「env 里那份是什么」。
- **变困难 / 必须记住**：
  - **装完不能直接用。** 生成会明确报「还没有任何模型配置」。验收清单、README、
    新机部署脚本都必须先建一条。
  - **表坏了就是不可用。** 以前「表损坏」会静默退回 env，现在会明确失败。
    这是有意的：静默降级让人以为配置生效了，而实际跑的是另一个东西。
  - **测试必须显式造配置。** 一批原本靠 env 默认值的用例（`test_llm_resilience`、
    `test_generation_job`、`test_observability`、`test_token_budget`、`test_runtime_config`）
    改成了「先 `ModelProviderRepository.create(...)` 再断言」。这不是用例啰嗦，
    而是真实前提：全新库上本来就是空的。
- **测试陷阱（真踩过）**：
  - 「改坏也不红」的一种形态：把回退删掉后，那些**只断言「拿到了 provider」**的用例
    会在有配置时照样绿。所以新增用例钉的是「一条都没有时 `None` + 报错文案」，
    而不是「能拿到东西」。
  - `test_default_cap_leaves_headroom_for_real_prompts` 曾因为「没有配置 → 32k 兜底」
    假失败：物理顶变了，断言却只看 `exceeded`。这类用例要显式声明自己依赖哪条配置。

---
