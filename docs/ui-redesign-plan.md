# HarnessPRD 前端 UI 设计评审与重构方案

> 评审范围：`frontend/src` 下全部 14 个组件、全局样式与构建配置
> 评审视角：UI 设计师（视觉系统 / 组件化 / 一致性 / 无障碍 / 主题）
> 结论先行：**架构与交互细节到位，但「视觉层」没有系统化，呈现典型的"功能可用、视觉粗糙"状态。**

---

## 一、现状评分（按维度）

| 维度 | 评级 | 说明 |
|------|------|------|
| 信息架构 / 流程编排 | **A-** | 入口→表单→对话→摘要闸门→PRD→RAG闸门→接口文档→提示词→完成，状态机清晰；`stages/` 拆分合理 |
| 交互细节（加载/空/错/焦点） | **B+** | 思考态轮播提示、滚动贴底、生成失败回退、二次确认删除都做了；但加载/Toast 各自为政 |
| 视觉一致性（设计系统） | **C-** | **核心问题**：无令牌、无组件库，样式全靠行内 utility 复制 |
| 组件可维护性 | **D** | 主按钮渐变复制 12 处、次按钮复制 10+ 处；`RequirementsForm.vue` 单体 **1768 行** |
| 无障碍（a11y） | **C** | 有 `aria-label`/`role`，但 `focus-visible` 不统一、无 `prefers-reduced-motion`、小字对比度临界 |
| 暗色 / 主题 | **F** | 无任何 `dark:` 变体，根元素渐变硬编码绑定浅色 |

架构（Vue3 + Vite8 + TS + Tailwind v4 + Pinia + vue-router）是健康的，问题全部集中在**表现层没有抽象**。

---

## 二、主要问题（均带代码证据）

### 1. 没有任何设计令牌 / CSS 变量系统
所有视觉决策散落在 `.vue` 行内 class。改主色要全局 grep，换肤/暗色无从谈起。
当前唯一"令牌"是 `tailwind.config.js` 里的 `primary: sky` 映射，但连它都没被完整遵守（见 #3）。

### 2. 没有组件库，基础元件被大量复制
- **主按钮渐变** `bg-gradient-to-r from-primary-500 to-primary-600 …` —— 全项目 **12 处**，且 padding 各异（`px-4 py-2` / `px-3 py-1.5` / `px-4 py-1.5` / `px-3 py-2.5`）。
- **次按钮** `rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 … hover:border-primary-400 hover:text-primary-700` —— **10+ 处**。
- 无 `<AppButton>` / `<AppInput>` / `<AppCard>` / `<AppBadge>` / `<AppDialog>` / `<AppToast>` / `<AppSpinner>` / `<AppIcon>`。
- 结论：任何一次品牌调整 = N 处手工同步，必然漂移。

### 3. 颜色漂移：`sky-*` 字面量 vs `primary-*` 令牌混用（13 处）
`primary` 在配置里就是 `sky`，二者同色却写法分裂，且同一元素内就混用：
- `AppV2.vue:362` `from-primary-500 to-sky-600`（同一元素一半令牌一半字面量）
- `DoneStage.vue` PRD 卡 `bg-primary-50` vs 接口文档卡 `bg-sky-50`
- `SolutionSidebar.vue:173` `bg-sky-50 ring-1 ring-sky-200`、`SummaryDiffPanel.vue:55` `bg-sky-50 text-sky-700 ring-sky-200`、`RagResultPanel.vue` 用 `sky-` 系
- 根背景 `AppV2.vue:352` `from-sky-50 via-slate-50 to-slate-100`
> 影响：想换品牌色时，grep `sky-` 才能找全；同色异写让维护者分不清"是不是故意不同"。

### 4. 渐变滥用 → 视觉廉价感
每个主操作都是"渐变 + 彩色阴影"，是典型 AI 生成审美，缺乏主次。专业做法是**主操作纯色填充 + 轻 elevation**，渐变只留给品牌/英雄时刻。

### 5. 字阶 / 间距 / 圆角 / 阴影无统一尺度
- 圆角混用 `rounded-2xl`(卡片) / `rounded-xl`(按钮/图标) / `rounded-lg`(小按钮) / `rounded-full`(徽章)，无明确映射。
- 阴影混用 `shadow-sm` / `shadow-md` / `shadow-lg shadow-sky-500/30` / `shadow-md shadow-primary-500/10`。
- 字号随用随取（`text-xl`/`text-base`/`text-sm`/`text-xs`），无"标题/正文/辅助"语义层级。

### 6. 无暗色模式
开发者工具，暗色是刚需。当前根 `min-h-screen bg-gradient-…` 硬编码浅色，加暗色需重构背景与全部 `slate` 文本配色。

### 7. 无图标系统
同一组 SVG path（对勾、箭头、下载、加号、用户）在 3–6 个文件里各复制一份。改一个图标 = 多处手工改。

### 8. 没有统一的反馈/浮层基础件
- `flashSavedTip` 在 `AppV2.vue` 内联一个 emerald 药丸 `Transition`，不可复用、非标准 Toast。
- 加载态各自实现：`ChatInput` 内联 `animate-spin`、`MessageList` 内联 `animate-bounce` 三点、`ProgressSteps` 内联 `animate-pulse`。
- 弹窗只有 `AuthDialog` 一个手写 Teleport，无 `<AppDialog>` 底座。

### 9. `RequirementsForm.vue` 单体 1768 行
已抽象出 `FieldConfig`/`SectionConfig`，但模板仍**逐类型手写重复 markup**，是维护与视觉统一的重灾区。

### 10. 无障碍缺口
- `focus-visible` 环：仅表单提交按钮有 `focus:ring-4 focus:ring-primary-200`，其余按钮靠默认 outline 或没有。
- 全项目 **0 处** `prefers-reduced-motion` 处理；`animate-pulse/bounce/spin` 较多。
- `slate-400` 小字（占位符、辅助说明）在白底对比度约 3.1:1，未达 AA 4.5:1。

---

## 三、重构目标与原则

1. **令牌驱动**：Tailwind v4 `@theme` + CSS 变量，所有颜色/字阶/间距/圆角/阴影/动效集中定义。
2. **原子组件层**：凡出现 ≥2 次的基础 UI 一律抽象为 `<AppXxx>`。
3. **暗色模式**：令牌双套 + `class="dark"` 切换，全量 `dark:` 适配。
4. **去廉价化**：收敛渐变（仅品牌区使用），用 elevation + 微交互建立质感。
5. **无障碍 WCAG AA**：统一 `focus-visible`、加 `reduced-motion`、修正临界对比度。
6. **增量迁移**：保留现有 203 例前端测试，逐组件替换，不重写业务逻辑。

---

## 四、设计系统规范（提案）

### 4.1 颜色令牌（`@theme` + `:root` / `.dark`）
```
语义色（业务只用这些，不直接写 sky-/slate-）：
--color-brand-50..950   ← 现 primary，统一收口，删掉散落 sky-*
--color-surface         ← 卡片/对话框底（浅 #fff / 暗 #0f172a）
--color-surface-muted   ← 次级底（浅 #f8fafc / 暗 #1e293b）
--color-border          ← 统一描边（浅 #e2e8f0 / 暗 #334155）
--color-text            ← 主文本（浅 #0f172a / 暗 #e2e8f0）
--color-text-muted      ← 辅助文本（浅 #475569 提亮以满足 AA / 暗 #94a3b8）
--color-text-subtle     ← 占位（浅 #64748b / 暗 #64748b）
--color-success/-warning/-danger/-info  ← 语义状态色（统一一套）
```
> 落地动作：把 13 处 `sky-*` 字面量用脚本批量替换回 `brand-*`，再删除 `tailwind.config.js` 里的 sky 别名。

### 4.2 字阶（Typography）
| Token | 尺寸 | 用途 |
|-------|------|------|
| `--text-display` | 30px/1.2 | 完成页大标题 |
| `--text-title` | 20px/1.3 | 区块标题 |
| `--text-subtitle` | 16px | 卡片标题 |
| `--text-body` | 14px | 正文/输入 |
| `--text-caption` | 12px | 辅助/徽章 |
| `--text-micro` | 11px | 路径/时间戳 |
字体：`--font-sans: 'Inter', system-ui, sans-serif`（引入 Inter，中文回退系统黑体）。

### 4.3 间距 / 圆角 / 阴影尺度
- 间距沿用 4px 基准（4/8/12/16/24/32/48），但**禁止**再出现 `px-3.5 py-2.5` 这类"随手值"，统一进 token。
- 圆角：卡片 `xl(12)`、按钮/输入 `lg(8)`、徽章 `full`、图标容器 `md(6)`。
- 阴影（elevation 三级）：`--shadow-1`（卡片静止）/ `--shadow-2`（悬浮）/ `--shadow-3`（弹窗）。

### 4.4 动效令牌（含 reduced-motion）
```
--motion-fast 150ms / --motion-base 250ms / --motion-slow 400ms
--ease-standard cubic-bezier(.4,0,.2,1)
全局：@media (prefers-reduced-motion: reduce){ * { animation: none !important; transition: none !important } }
```

---

## 五、组件库清单（新建 `components/ui/`）

| 组件 | 替代对象 | 关键变体 |
|------|----------|----------|
| `AppButton` | 12 处主按钮 + 10+ 处次按钮 | `variant: primary/secondary/ghost/danger`、`size: sm/md`、`loading` 内置 spinner、`icon` 插槽 |
| `AppInput` / `AppTextarea` | 表单全部输入 | `invalid` 态、统一 focus 环 |
| `AppSelect` | 平台/认证/存储下拉 | 原生 + datalist 兼容 |
| `AppCard` / `AppPanel` | 各 stage 的 `rounded-2xl border … shadow-sm` | `padding`、`hover` 开关 |
| `AppBadge` | 产出标签、阶段徽章 | `tone: neutral/brand/success/…` |
| `AppIcon` | 所有内联 SVG path | 图标名注册表（check/arrow/plus/download/user/…） |
| `AppDialog` | `AuthDialog` 手写 Teleport | `title`/`footer` 插槽、聚焦陷阱、Esc 关闭 |
| `AppToast` | `flashSavedTip` 内联药丸 | `type`、自动消失、可堆叠 |
| `AppSpinner` | `animate-spin`/`animate-bounce` 内联 | `size` |
| `AppEmpty` / `AppStateBlock` | 侧栏/消息列表/文档区空态 | `icon`/`title`/`description` |

> 优先级：**AppButton → AppIcon → AppCard → AppToast**，这 4 个能吃掉 70% 的重复样式。

---

## 六、迁移路线图（分阶段，每阶段可独立验证）

- **P0 打底**：`index.css` 引入 `@theme` 令牌；建 `styles/tokens.css` 与 `styles/base.css`（含 reduced-motion、统一 focus-visible、字体）；全量替换 `sky-*`→`brand-*`。
- **P1 基础件**：实现 `AppButton`/`AppIcon`/`AppCard`/`AppToast`/`AppSpinner`/`AppBadge`，配单测 + Vitest 截图样张。
- **P2 业务件**：
  - 拆 `RequirementsForm` → `FormFieldRenderer`（读 config 渲染）+ `fields/{TextField,ListField,MvpField,PagesField}.vue`，消灭 1768 行单体。
  - `AuthDialog`、`flashSavedTip`、`RagResultPanel`、`SummaryDiffPanel` 改用 `AppDialog`/`AppToast`。
  - 入口页/对话页/审阅页用 `AppButton`/`AppCard` 替换行内样式。
- **P3 暗色 + 无障碍**：全量 `dark:` 适配、暗色切换入口（`UserMenu`）、对比度修正、`focus-visible` 统一。
- **P4 收口**：更新 `tailwind.config.js`、写组件使用示例（handoff 文档）、用无头浏览器对关键页截图验证。

---

## 七、风险与权衡

- **渐变改纯色可能显得平淡** → 用 elevation 分级 + hover 微交互 + 品牌色点状高亮补偿质感。
- **拆大表单回归风险** → 保留 203 例测试，逐字段迁移、每迁一个字段跑一次；`FieldConfig` 结构不变，只换渲染层。
- **批量替换 `sky-*` 误伤** → 用脚本仅替换"颜色语义"用法的 `sky-`，保留非语义极少处；替换后跑 `npm run build` + 截图比对。
- **暗色工作量** → 令牌双套后，绝大多数组件只需在 `dark:` 下重设 surface/border/text 三组，可控。

---

## 八、建议的下一步

我建议先落地 **P0 + P1 的最小可用样板**：搭好令牌与一个入口页（EntryStage）的重构样例，跑通后再批量迁移其余页面。这样你能先看到"系统化后长什么样"，再决定是否全量推进。

如需我直接动手，请确认从哪一步开始（推荐：先 P0 令牌 + AppButton/AppIcon/AppCard 三个底座 + 入口页样板）。
