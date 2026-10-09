/**
 * Markdown 渲染工具。
 *
 * 聊天里的 AI 回复与后续的文档预览都需要把 Markdown 渲染成 HTML，
 * 统一在这里维护一个 markdown-it 实例，避免每条消息重复创建解析器。
 */
import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({
  // 关闭原始 HTML：内容来自模型输出与用户输入，避免 XSS
  html: false,
  // 自动识别裸链接
  linkify: true,
  // 单个换行转成 <br>，符合聊天里的输入直觉
  breaks: true,
  typographer: false,
})

// 外链在新标签页打开，并加 noopener，防止目标页通过 window.opener 反向操作
const defaultLinkOpen =
  md.renderer.rules.link_open ??
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const token = tokens[idx]
  if (token) {
    token.attrSet('target', '_blank')
    token.attrSet('rel', 'noopener noreferrer')
  }
  return defaultLinkOpen(tokens, idx, options, env, self)
}

/**
 * 把 Markdown 文本渲染成 HTML 字符串。
 *
 * @param text Markdown 源码；空值返回空串，便于模板里直接 `v-html`。
 */
export function renderMarkdown(text: string): string {
  if (!text) return ''
  return md.render(text)
}
