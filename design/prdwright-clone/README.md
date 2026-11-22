# PRDwright 用户端 · 前端复刻稿

把 `frontend/`（Vue 3 + Vite + Tailwind v4）里的真实界面，复刻成一屏一个静态 HTML，放进右侧「设计工作台」。
**不是「照感觉画一版」**：样式表是真实构建产物，类名照抄源码，所以颜色、间距、字号、圆角、暗色都是原件那份。

## 为什么这么做

| 原件 | 复刻稿怎么用 |
| --- | --- |
| `frontend/src/index.css` 的三层设计令牌 | 抽成 `tokens.css`（取值逐字相同），改这一个文件所有屏一起变 |
| `frontend/dist/assets/index-*.css`（Tailwind 编译产物，73 KB） | 整份存为 `app.css`，页面直接引用 |
| 各 `.vue` 的 `<template>` | 一屏一个 `screens/<名>.body.html`，class 字符串原样搬过来 |
| 自研组件（AppButton / AppCard / AppIcon / AppActionBar …） | 按组件的真实模板展开成 HTML（类名同样照抄） |

好处：写的是 HTML，长得和真站一致，不用起后端、不用连数据库；改一处令牌，所有屏同步。
代价：`app.css` 只含源码里**用过**的类名，手写一个源码没有的类名不会报错、只会静默失效 ——
所以每屏生成后都要跑 `node check-classes.mjs`。

## 目录

```
tokens.css            设计令牌（颜色 / 圆角 / 字体 / 外壳几何），唯一的风格开关
app.css               真实 Tailwind 产物，勿改
build.mjs             把「正文片段 + 共用外壳」拼成整页（含进度条、侧栏）
check-classes.mjs     类名体检：找出 app.css 里没有的类名
parts/sidebar.html        左侧方案列表（登录态）
parts/sidebar-guest.html  左侧方案列表（未登录态）
screens/<名>.body.html    这一屏 <main> 里的内容
screens/<名>.body.json    标题、进度条状态、吸底栏高度、是否暗色、底图 / 复用哪一屏
<名>.html                 产物，工作台打开的就是它
```

改完片段后跑：

```bash
node build.mjs                 # 重新拼所有屏
node check-classes.mjs         # 类名体检（全绿才算接上样式）
```

`body.json` 支持的字段：`title`、`progress{current,reached,generating}`、`barHeight`、
`dark`、`sidebar:"guest"`、`underlay`（浮层底图复用另一屏）、`bodyFrom`（整屏正文复用另一屏，暗色变体用）。

## 屏与源码的对应

| 屏幕 | 状态 | 源码 |
| --- | --- | --- |
| `entry.html` | 新建方案、四入口两组 | `stages/EntryStage.vue` + `utils/workflow.ts` 的 `ENTRY_REGISTRY` |
| `auth.html` | 登录弹窗盖在入口页上 | `AuthDialog.vue` |
| `form.html` | 完整流程的结构化需求表单（区块 1、2 展开） | `RequirementsForm.vue` |
| `chat.html` | 需求澄清对话（消息 + 结构化选择题 + 输入框） | `stages/ChatStage.vue`、`MessageList.vue`、`ClarificationAskList.vue`、`ChatInput.vue`、`ChatThreadTabs.vue` |
| `generating.html` | PRD 逐章生成中 | `stages/GeneratingStage.vue`、`ui/StageOutputStream.vue`、`ui/DocPhaseProgress.vue` |
| `review.html` | PRD 待确认（审核结论 + 正文 + 操作栏） | `stages/ReviewStage.vue`、`DocumentReview.vue`、`ui/ReviewConclusionPanel.vue` |
| `review-dark.html` | 同一屏的暗色主题 | 同上（正文复用 `review`，外壳加 `.dark`） |
| `done.html` | 三份文档全部交付 | `stages/DoneStage.vue` |
| `quick.html` | 快捷流程：一段话直接生成 | `stages/QuickPrdStage.vue` |
| `import.html` | 导入素材：PRD + 接口文档两栏粘贴 | `stages/PastePairStage.vue` |

八屏讲的是**同一份方案**「社区二手交易 App」：入口 → 表单 → 对话 → 生成 → 审阅（含暗色）→ 交付，
侧栏列表里其它方案只是列表内容，点进去仍落在已有屏上（静态稿没有按 id 路由）。

## 文案纪律（改这套稿子时照这个来）

这一版在复刻的基础上做过一次文案与可读性清理，规则如下，别再往回加：

1. **不写标语**：品牌名下面那句「需求 → PRD / 接口 / 提示词」、页面标题下的说明段、卡片组的副标题，
   一律不写。标题 + 分组 + 卡片本身已经说清「这是什么、该选哪个」。
2. **不写解释已知内容的提示句**：按钮有「下载」就不用写「可分别下载为 Markdown」；
   输入框就在眼前就不用写「若不满意选项，可以自行在输入框输入想法」。
   真正的前置条件、破坏性动作的后果（如「停止后已写出的内容会保留」）留着——那是信息，不是填充。
3. **次要标签不小于 12px**：不再用 `text-[11px]` / `text-[10px]` 硬塞。要放不下就改布局，
   别缩字号（进度条标签、严重度徽标、请求号、必填徽标都已抬到 `text-xs`）。
4. **看不明白先改布局**：例如入口页原来靠一段说明解释四入口的区别，现在靠「两组四卡 + 组标题」的排布；
   审核页的问题描述把行长收到 41 字（`max-w-xl`），而不是缩小字号。
5. **颜色走语义令牌**：必填星号与「必填」徽标用 `text-danger-fg` / `bg-danger-surface` / `ring-danger-line`，
   不再散写 `text-red-500` / `bg-red-50`；暗色下由令牌自动换值。

## 已知偏差（有意为之）

- **吸底操作栏高度**：开发时由 `AppActionBar` 用 ResizeObserver 写入 `--shell-action-bar-h`；
  静态稿按实测值写进 `barHeight`（只有流程行约 61–63，带内容工具行 114，带输入框 138）。
- **对话页消息区高度**：源码是 `min(60vh, 560px)`，这里钉成 400px，
  为的是在 1280×800 画幅里让「提交回答」落在操作栏之上；页面仍比一屏长（828px），要滚动。
- **表单把第 2 区块也展开了**（真实产品默认只展开第 1 块），为了让 MVP 编辑器与页面卡编辑器都能被看见；
  代价是这一屏高 4812px。想还原默认态：把 `screens/form.body.html` 里第 2 个 `section` 的展开标记去掉即可。
- **无脚本**：静态稿不写 JS。按钮、卡片是 `<a href>`，在工作台里点得动；悬停 / 聚焦状态由 CSS 负责。

## 相对原件做的可读性修正

| 位置 | 原件 | 这里 | 为什么 |
| --- | --- | --- | --- |
| 侧栏品牌 | 11px 副标题 | 去掉 | 标语；去掉后视力与层级都更好（也是体检报的那条） |
| 进度条标签 / 严重度徽标 / 请求号 | 10–11px | 12px（`text-xs`） | 小字不可读；布局放得下 |
| 必填星号、「必填」徽标 | `text-red-500` 3.5–3.8:1 | `text-danger-fg` 7.6:1 | 语义令牌 + AA 达标 |
| 计数徽标 | `opacity-75` 4.4:1 | 去掉透明 5.9:1 | 同上 |
| 模块 chip 的「×」 | 8×16 | 24×24 | 点击区 |
| 暗色 `--ui-on-accent` | `#2b1508` 2.9:1 | `#ffffff` 5.9:1 | 深棕压在陶土上不可读 |
| 暗色 `--ui-subtle` | `#9d9488` 4.3:1 | `#a8a29a` 5.2:1 | 列表副信息在半透明底上偏糊 |
| 审核问题描述 | 一行 48–49 字 | 44 字（`max-width: 44em`） | 只收文字列，左边那根规则线仍由 `ul` 画满 |
| 文档正文（PRD） | 一行 46–49 字 | ≈44 字（`padding-inline: clamp(1.25rem, 8vw, 3.75rem)`） | 卡片保持满宽，靠内边距做版心，窄屏自动收紧 |
| 窄屏进度条 | 溢出屏幕 | 横向可滚 | 390px 下第 5/6 格本来会被切掉 |

### 行长怎么改（下次照这个来）

正文行长不要去改 `max-w-3xl`（那是整页骨架，一改所有卡片都变窄），也不要自造 Tailwind 类
（`app.css` 里没有的类会静默失效）。两处用的是同一招：

- **只收文字列**：块级元素上加 `style="max-width: 44em"`（`em` 跟着该元素的 `text-sm`/`prose-sm` 走 = 14px，44em ≈ 616px ≈ 44 个汉字）。
- **卡片满宽 + 版心**：`style="padding-inline: clamp(1.25rem, 8vw, 3.75rem)"`，1280 下左右各 60px、390 下各约 31px。

## 体检现状

- **1280×800 / 1440×900：0 问题**（10 屏全过，跳转全部可达）。
- **390×844 还剩 3 类**，都不是这版新引入的：
  文档卡与生成页正文是**内部滚动**区域，体检按矩形比对会把它和上方的标题判成「叠在一起」，
  截图看没有压字；`非功能要求` 表头里的「可用性」在窄栏下折成两行（三列表格的正常表现）。
  要较真就得给正文列做窄屏专用样式，那属于移动端适配，不在这次范围内。



