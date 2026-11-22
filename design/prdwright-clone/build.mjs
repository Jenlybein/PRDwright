/**
 * 把「每屏的正文片段」拼成完整 HTML。
 *
 * 为什么要有这一步：这套复刻稿有八屏，而侧边栏、进度条、外壳（flex + max-w-3xl 的 main）
 * 在每一屏上都必须一模一样。八份手抄的侧边栏迟早会漂移，所以共用部分只写一份：
 *   parts/sidebar.html            左侧方案列表（逐字抄自 SolutionSidebar.vue）
 *   screens/<名>.body.json        标题、进度条状态、吸底栏高度
 *   screens/<名>.body.html        这一屏 <main> 里的内容（进度条之后、</main> 之前）
 * 产物 <名>.html 是设计工作台真正打开的文件；改共用部分后重新 `node build.mjs` 即可。
 *
 * 只依赖 Node 内置模块，不需要装任何东西。
 */
import { readdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = dirname(fileURLToPath(import.meta.url))
const SCREENS = join(ROOT, 'screens')

/** 进度条：顺序即执行顺序（同 frontend/src/types/index.ts 的 STEPS） */
const STEP_ORDER = ['form', 'chatting', 'review-prd', 'review-api-docs', 'review-prompts', 'done']
const STEP_LABELS = {
  form: '描述产品',
  chatting: 'AI 对话',
  'review-prd': 'PRD',
  'review-api-docs': '接口文档',
  'review-prompts': '提示词',
  done: '交付',
}
/** 被摘到最左边的「对话区」步骤 */
const SIDE_STEP = 'chatting'
const CHAT_ICON = 'M6 1.5h8a2.5 2.5 0 0 1 2.5 2.5v6a2.5 2.5 0 0 1-2.5 2.5H9l-5.5 2.6V4A2.5 2.5 0 0 1 6 1.5Z'
const CHECK_ICON =
  'M16.704 5.29a1 1 0 0 1 .006 1.414l-7.5 7.5a1 1 0 0 1-1.414 0l-4-4a1 1 0 1 1 1.414-1.414l3.293 3.293 6.793-6.793a1 1 0 0 1 1.408 0Z'

/**
 * 渲染进度条（逐字对应 ProgressSteps.vue 的三元分支）。
 *
 * @param {{ current?: string, reached?: string, generating?: boolean }} state
 */
function renderProgress(state) {
  if (!state || !state.current) return ''
  const currentIndex = STEP_ORDER.indexOf(state.current)
  const reachedIndex = STEP_ORDER.indexOf(state.reached ?? state.current)
  if (currentIndex < 0 || reachedIndex < 0) throw new Error(`未知步骤：${state.current} / ${state.reached}`)

  // 画面顺序：对话那一步排到最左，其余里程碑按原顺序，序号只在里程碑之间数
  const items = []
  for (let index = 0; index < STEP_ORDER.length; index += 1) {
    const id = STEP_ORDER[index]
    if (id === SIDE_STEP) items.push({ id, index, ordinal: null, kind: 'side' })
  }
  let ordinal = 0
  for (let index = 0; index < STEP_ORDER.length; index += 1) {
    const id = STEP_ORDER[index]
    if (id === SIDE_STEP) continue
    ordinal += 1
    items.push({ id, index, ordinal, kind: 'milestone' })
  }
  const milestoneCount = ordinal

  const lis = items
    .map((item) => {
      const isCurrent = item.index === currentIndex
      const reached = item.index <= reachedIndex
      // 圆点配色：当前（生成中 / 非生成中）> 已到达 > 未到达
      const circleClass = isCurrent
        ? state.generating
          ? 'bg-accent-soft text-accent ring-2 ring-accent animate-pulse'
          : 'bg-primary-500 text-on-accent ring-4 ring-accent-soft'
        : reached
          ? 'bg-primary-500 text-on-accent'
          : 'bg-surface-2 text-subtle'
      const inner =
        item.kind === 'side'
          ? `<svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="${CHAT_ICON}" clip-rule="evenodd" /></svg>`
          : reached && !isCurrent
            ? `<svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="${CHECK_ICON}" clip-rule="evenodd" /></svg>`
            : `${item.ordinal}`
      const labelClass = reached ? 'text-accent' : 'text-subtle'
      const liClass =
        item.kind === 'side'
          ? 'mr-4 flex items-center border-r border-line-strong pr-4'
          : 'flex flex-1 items-center'
      const connector =
        item.kind === 'milestone' && item.ordinal !== null && item.ordinal < milestoneCount
          ? `
        <div class="mx-1 mb-5 h-0.5 flex-1 overflow-hidden rounded-full bg-line-strong">
          <div class="h-full origin-left bg-primary-500 transition-[clip-path] duration-200 ease-[var(--ease-smooth)]" style="clip-path: ${item.index < reachedIndex ? 'inset(0 0 0 0)' : 'inset(0 100% 0 0)'}"></div>
        </div>`
          : ''
      return `
      <li class="${liClass}">
        <div class="flex w-16 shrink-0 flex-col items-center gap-1.5${reached ? ' cursor-pointer group' : ''}">
          <span class="flex h-8 w-8 items-center justify-center rounded-full text-xs font-semibold tabular-nums transition ${circleClass}${reached ? ' group-hover:ring-4 group-hover:ring-accent-soft-line' : ''}">${inner}</span>
          <span class="whitespace-nowrap text-xs font-medium transition ${labelClass}${reached ? ' group-hover:text-accent' : ''}">${STEP_LABELS[item.id]}</span>
        </div>${connector}
      </li>`
    })
    .join('')

  return `
    <nav class="sticky top-0 z-20 -mx-4 mb-6 border-b border-line bg-bg px-4 pb-3 pt-3" style="overflow-x: auto" aria-label="生成进度">
      <ol class="flex items-start">${lis}
      </ol>
    </nav>`
}

const sidebar = await readFile(join(ROOT, 'parts', 'sidebar.html'), 'utf8')
const sidebarGuest = await readFile(join(ROOT, 'parts', 'sidebar-guest.html'), 'utf8')

const files = (await readdir(SCREENS)).filter((name) => name.endsWith('.body.json'))
const written = []
for (const file of files) {
  const name = file.replace(/\.body\.json$/, '')
  const meta = JSON.parse(await readFile(join(SCREENS, file), 'utf8'))
  // underlay：这一屏是浮层（如登录弹窗），底图复用另一屏的正文，免得手抄第二份
  const underlay = meta.underlay
    ? await readFile(join(SCREENS, `${meta.underlay}.body.html`), 'utf8')
    : ''
  // bodyFrom：整屏正文复用另一屏（暗色变体就是同一份正文 + <html class="dark">）
  const ownBody = meta.bodyFrom
    ? await readFile(join(SCREENS, `${meta.bodyFrom}.body.html`), 'utf8')
    : await readFile(join(SCREENS, `${name}.body.html`), 'utf8')
  const body = `${underlay}\n${ownBody}`
  const shell = meta.sidebar === 'guest' ? sidebarGuest : sidebar
  const barHeight = meta.barHeight ?? 0
  const html = `<!DOCTYPE html>
<html lang="zh-CN"${meta.dark ? ' class="dark"' : ''}>
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="color-scheme" content="light dark" />
    <meta name="dsh-design" content="web" />
    <title>${meta.title}</title>
    <link rel="stylesheet" href="app.css" />
    <link rel="stylesheet" href="tokens.css" />
    <style>
      :root {
        --shell-action-bar-h: ${barHeight}px;
      }
    </style>
  </head>
  <body class="bg-bg text-body">
    <div class="flex min-h-screen bg-bg text-body">
      ${shell.trim()}

      <div class="flex min-w-0 flex-1 flex-col">
        <main class="mx-auto w-full max-w-3xl px-4 pt-10 pb-[calc(var(--shell-action-bar-h)+2.5rem)]">
          ${renderProgress(meta.progress).trim()}
          ${body.trim()}
        </main>
      </div>
    </div>
  </body>
</html>
`
  await writeFile(join(ROOT, `${name}.html`), html, 'utf8')
  written.push(`${name}.html`)
}

console.log(`已生成 ${written.length} 屏：${written.join(', ')}`)
