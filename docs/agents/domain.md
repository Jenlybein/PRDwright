# 领域文档

工程类 skill 在动手探索代码之前，先按这里的规定读领域文档。

## 探索前先读这些

- **`GLOSSARY.md`**（仓库根）：本仓库是**单上下文**仓库，没有 `GLOSSARY-MAP.md`，术语只有这一份。写 issue 标题、重构提案、假设、测试名时用它的词。
- **`docs/adr/`**：面向你即将改动的区域读相关决策。**编号是全局连续的 `ADR-001` ~ `ADR-097`**，编号本身就是跨层引用锚点；`docs/architecture/v*/ADR.md` 是存档副本，不再是维护入口。
- **`docs/agents/conventions.md`**：当前有效的工程约定与已知的坑（分层、迁移、SSE、版本链、测试、部署）。改代码前读，比翻 ADR 快。

任何一份不存在就**静默跳过**——不要提示缺失，也不要主动张罗着去创建。`GLOSSARY.md` 与 ADR 由 `/domain-modeling` 在术语或决策真正定下来时增量更新，不预先堆砌。

## 文件结构

```text
/
├── GLOSSARY.md                 ← 单上下文术语表（唯一一份）
├── docs/
│   ├── adr/                    ← ADR-001 ~ ADR-099，一条一文件 + README 索引
│   ├── agents/                 ← 本目录：agent 的消费规则与约定
│   │   ├── domain.md           ← 你正在读的这份
│   │   ├── issue-tracker.md
│   │   ├── triage-labels.md
│   │   └── conventions.md
│   ├── architecture/           ← 按层归档 v1 ~ v8，每层 README / ARCHITECTURE / roadmap 三份
│   │   └── README.md           ← 总览：该看哪层 + 阶段总表 + 已确认的失真
│   ├── journal/                ← 历史日记（旧工具工作区迁入，仅存档）
├── backend/  frontend/  frontend_admin/  skills/  scripts/
```

## 用词表里的词

输出里要命名领域概念时（issue 标题、重构提案、假设、测试名），用 `GLOSSARY.md` 里的写法，**不要**滑向它明确列为 `_Avoid_` 的同义词。典型例子：说「方案」不说「项目」，说「被取代」不说「中止」，说「恢复」不说「回滚」。

需要的概念词表里没有，这本身就是个信号：要么你在发明这个项目不用的语言（重新想想），要么真有个空洞（记下来交给 `/domain-modeling`）。

## 撞上 ADR 就摆到台面上

如果你的结论和某条 ADR 冲突，明确说出来，不要默默覆盖：

> _与 ADR-091（SQLite 仍是默认后端）冲突，但值得重开，因为……_

同理，`docs/architecture/README.md` §2 的阶段总表（**时间线真源**）与某份架构文档说法不一致时，**以时间线真源为准**，并把失真记进那张「已确认的失真」表。
