/**
 * 类名体检：确认每一屏用到的 Tailwind 类名都在 app.css 里真的编译出来了。
 *
 * 为什么需要它：app.css 是 frontend/dist 里那份**已编译**的产物，只包含源码里用过的类名。
 * 复刻稿里若手写了一个源码中不存在的类名（`p-4.5`、`text-[13px]` 之类），页面不会报错，
 * 只是那条样式静默失效——看起来「差一点」，很难查。这个脚本把这些类名逐个揪出来。
 *
 * 用法：node check-classes.mjs            体检全部生成的页面
 *       node check-classes.mjs form.html  只体检一屏
 */
import { readFile, readdir } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = dirname(fileURLToPath(import.meta.url))
const css = await readFile(join(ROOT, 'app.css'), 'utf8')

/** CSS.escape 的等价实现：把选择器里需要转义的字符加上反斜杠 */
function cssEscape(value) {
  let out = ''
  for (const ch of value) {
    if (/[a-zA-Z0-9_-]/.test(ch)) out += ch
    else if (ch === '\0') out += '\uFFFD'
    else out += `\\${ch}`
  }
  return out
}

const targets = process.argv.slice(2)
const files = targets.length
  ? targets
  : (await readdir(ROOT)).filter((name) => name.endsWith('.html'))

let totalMissing = 0
for (const file of files) {
  const html = await readFile(join(ROOT, file), 'utf8')
  const classes = new Set()
  for (const match of html.matchAll(/class="([^"]*)"/g)) {
    for (const token of match[1].split(/\s+/)) if (token) classes.add(token)
  }
  const missing = [...classes].filter((token) => !css.includes(`.${cssEscape(token)}`))
  if (missing.length === 0) {
    console.log(`✅ ${file}：${classes.size} 个类名全部有样式`)
    continue
  }
  totalMissing += missing.length
  console.log(`❌ ${file}：${missing.length} 个类名在 app.css 里找不到`)
  for (const token of missing) console.log(`     ${token}`)
}

if (totalMissing > 0) process.exitCode = 1
