# Triage 标签

工程类 skill 只用五个固定角色说话，不看标签的实际拼写。这张表把角色映射到本仓库 issue tracker 里真正用的字符串。

| 角色 | 本仓库标签 | 含义 | 谁推进下一步 |
| --- | --- | --- | --- |
| `needs-triage` | `needs-triage` | 等维护者判断这是什么、要不要做 | 维护者 |
| `needs-info` | `needs-info` | 等报告者补信息，缺信息无法定级 | 报告者 |
| `ready-for-agent` | `ready-for-agent` | 已完全规格化，可以整包交给 AFK 的 agent 去做 | agent |
| `ready-for-human` | `ready-for-human` | 需要人来动手（判断、审美、外部凭据、线上操作） | 人 |
| `wontfix` | `wontfix` | 明确不做，理由写在 issue 评论里 | 无人 |

当某个 skill 提到某个角色（如「打上 AFK 就绪标签」），就用本表「本仓库标签」列里的字符串。

## 本仓库现状

- 五个标签已在本仓库创建完毕（描述为中文）。`wontfix` 是 GitHub 默认标签，**早已存在，未被改动**，直接复用。
- 现有一批 GitHub 默认标签（`bug` / `enhancement` / `documentation` / `question` / `duplicate` / `invalid` / `help wanted` / `good first issue` / `accessibility`）继续表示**问题性质**；triage 五个标签表示**流程状态**。两者正交，可以同时贴。
- 典型流转：新 issue `needs-triage` → 缺信息转 `needs-info` → 定级后转 `ready-for-agent` 或 `ready-for-human` → 关闭时确认理由，`wontfix` 用于明确不做。

要换词表（比如你更习惯 `bug:triage` 这种写法）就改「本仓库标签」这一列，然后 `gh label create` 补齐、`gh label delete` 收拾旧的；其他文件不用动。
