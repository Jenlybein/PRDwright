import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

/**
 * 可读性守卫（与设计稿 `design/prdwright-clone` 对齐的三条硬线）。
 *
 * 这三条都是「改一处看不出、漏一处就回到旧样」的规则，所以钉成红灯，而不是写在文档里
 * 指望下个人记得：
 *
 * 1. **字号下限 12px** —— 10/11px 的次要标签在中文里糊成一片；放不下就改布局，别缩字号。
 * 2. **必填类提示走语义令牌** —— 裸 `text-red-500` 在白底只有 3.6:1，改 `text-danger-fg` 是 7.6:1，
 *    而且暗色下令牌自己会换值，不必再写一组成对的 `dark:` 变体。
 * 3. **暗色主按钮文字必须可读** —— `--ui-on-accent` 一旦回退成深棕，陶土色主按钮上的字就只剩 2.9:1。
 */
const componentSources = import.meta.glob('../components/**/*.vue', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

describe('可读性守卫', () => {
  it('组件里不再出现 10px / 11px 的任意字号', () => {
    const offenders: string[] = []
    for (const [path, source] of Object.entries(componentSources)) {
      const matches = source.match(/text-\[1[01]px\]/g)
      if (matches) offenders.push(`${path}: ${matches.length} 处`)
    }
    expect(offenders).toEqual([])
  })

  it('必填类提示不再用裸 text-red-500 / text-rose-500（改走 text-danger-fg）', () => {
    const offenders: string[] = []
    for (const [path, source] of Object.entries(componentSources)) {
      if (/text-(red|rose)-500\b/.test(source)) offenders.push(path)
    }
    expect(offenders).toEqual([])
  })

  it('暗色主题的主按钮文字与次要文字取值符合对比度基线', () => {
    // 走 fs 直读源文件：`?raw` 经 Tailwind 插件处理后在测试环境里是空串；
    // 这里也不依赖 import.meta.url（jsdom 下不是 file: 协议），直接用项目根拼路径。
    const indexCss = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf8')
    expect(indexCss).toMatch(/\.dark\s*\{[^}]*--ui-on-accent:\s*#ffffff/i)
    expect(indexCss).toMatch(/\.dark\s*\{[^}]*--ui-subtle:\s*#a8a29a/i)
  })
})
