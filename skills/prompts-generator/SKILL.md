---
name: prompts-generator
description: 根据 PRD 产出一套可直接使用的 AI 提示词文档（Prompt 文档），与产品真实业务场景绑定。
applies_to: [prompts]
version: 0.1.0
---

# prompts-generator

根据 PRD 正文产出一套可直接使用的 AI 提示词文档（Prompt 文档）。提示词必须和产品的
真实业务场景绑定，不要生成通用的万能模板。

## 执行流程

1. 读取 PRD 正文，提炼其中适合用 AI 提示词落地的环节（如需求评审、内容生成、客服应答、质检等）。
2. 读取 `references/prompts-guide.md`，按其产出要求组织提示词套件：设计原则、按场景分组的
   提示词清单（含名称、完整正文、输入变量、预期输出）、变量约定与调优建议。
3. 每个提示词都要写清「角色、任务、约束、输出格式」四要素，正文用代码块包裹。

## 资源

- `references/prompts-guide.md`：提示词套件的产出要求，生成时作为用户消息模板（含 `{prd_content}` 占位符）。
