# 领域文档

工程类 skill 在动手探索代码之前，先按这里的规定读领域文档。

## 探索前先读这些

- **`GLOSSARY.md`**（仓库根）：本仓库是**单上下文**仓库，没有 `GLOSSARY-MAP.md`，术语只有这一份。写 issue 标题、重构提案、假设、测试名时用它的词。
- **`docs/architecture/ARCHITECTURE.md`**：唯一一版现状架构，读「设计」「契约」「取舍」节与三段承重附录；与既有架构决策冲突时以它为准。
- **`docs/adr/`**：决策档案（编号从 0001 重新开始），现有 0001「文档重组」与 0002「审核结论与修复」；不要预先堆砌新档案。
- **`docs/architecture/README.md`**：时间线真源（阶段总表 / v2 七波重写 / 已确认的失真 / 复核方法）。
- **`docs/architecture/roadmap.md`**：现状排期（#31~#41 各票落点与验收方式、#44~#49「入口模式收成一张登记表」与 #52~#59「审核结论与修复」两轮的票序、落点与实施状态）。
- **`docs/agents/conventions.md`**：当前有效的工程约定与已知的坑（分层、迁移、SSE、版本链、测试、部署）。改代码前读，比翻历史文档快。

任何一份不存在就**静默跳过**——不要提示缺失，也不要主动张罗着去创建。`GLOSSARY.md` 由 `/domain-modeling` 在术语真正定下来时增量更新；实施取舍写进 `docs/architecture/ARCHITECTURE.md` 的「取舍」节，跨层的架构决策才进 `docs/adr/`。

## 文件结构

```text
/
├── GLOSSARY.md                 ← 单上下文术语表（唯一一份）
├── docs/
│   ├── agents/                 ← 本目录：agent 的消费规则与约定
│   │   ├── domain.md           ← 你正在读的这份
│   │   ├── issue-tracker.md
│   │   ├── triage-labels.md
│   │   └── conventions.md
│   ├── adr/                    ← 决策档案（0001 文档重组、0002 审核结论与修复）
│   ├── architecture/           ← 单一现状文档 + 导航 / 时间线真源 + 现状排期
│   │   ├── ARCHITECTURE.md     ← 唯一一版现状架构（含承重附录）
│   │   ├── README.md           ← 导航 + 时间线真源（阶段总表、已确认的失真）
│   │   └── roadmap.md          ← 现状排期（#31~#41、#44~#49、#52~#59）
├── backend/  frontend/  frontend_admin/  skills/  scripts/
```

## 用词表里的词

输出里要命名领域概念时（issue 标题、重构提案、假设、测试名），用 `GLOSSARY.md` 里的写法，**不要**滑向它明确列为 `_Avoid_` 的同义词。典型例子：说「方案」不说「项目」，说「被取代」不说「中止」，说「恢复」不说「回滚」。

需要的概念词表里没有，这本身就是个信号：要么你在发明这个项目不用的语言（重新想想），要么真有个空洞（记下来交给 `/domain-modeling`）。

## 撞上既有架构决策就摆到台面上

如果你的结论和某条既有架构决策冲突（写在 `docs/architecture/ARCHITECTURE.md` 的「取舍」节、`docs/adr/` 或 issue 历史里），明确说出来，不要默默覆盖：

> _与「SQLite 仍是默认后端」这条既有决策冲突，但值得重开，因为……_

同理，`docs/architecture/README.md` §2 的阶段总表（**时间线真源**）与 `docs/architecture/ARCHITECTURE.md` 说法不一致时，**以时间线真源为准**，并把失真记进那张「已确认的失真」表。
