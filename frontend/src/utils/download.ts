/**
 * 文件下载工具。
 *
 * 纯前端生成并下载文本文件：把内容编码成 Blob，通过临时 <a> 触发浏览器下载。
 * 不依赖后端，适合导出 PRD / 接口文档 / 提示词套件这类 Markdown 文本。
 */

/**
 * 触发浏览器下载一个文本文件。
 *
 * @param content 文件正文（纯文本 / Markdown 源码）
 * @param filename 下载时使用的文件名（含扩展名，如 `prd.md`）
 * @param mimeType 文件 MIME 类型，默认 `text/markdown`
 */
export function downloadFile(
  content: string,
  filename: string,
  mimeType = 'text/markdown;charset=utf-8',
): void {
  // 内容为空时不下载，避免生成无意义的空文件
  if (!content) return

  const blob = new Blob([content], { type: mimeType })
  const url = URL.createObjectURL(blob)

  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  // 将锚点挂到 DOM 上再点击，兼容 Firefox 等需要元素在文档内才触发下载的浏览器
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()

  // 尽快释放对象 URL，避免内存占用
  URL.revokeObjectURL(url)
}
