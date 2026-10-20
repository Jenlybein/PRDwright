/**
 * 文件下载工具。
 *
 * 纯前端生成并下载文本文件：把内容编码成 Blob，通过临时 <a> 触发浏览器下载。
 * 不依赖后端，适合导出 PRD / 接口文档 / 提示词套件这类 Markdown 文本。
 */

/**
 * 触发浏览器下载一个 Blob。
 *
 * 与 `downloadFile` 分开是因为交付包是**二进制**（ZIP）而非文本：两者落盘动作完全相同，
 * 只是 Blob 的来源不同。抽出来避免 ZIP 那条路径再抄一遍 `<a>` 的创建与释放。
 */
export function downloadBlob(blob: Blob, filename: string): void {
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
  downloadBlob(new Blob([content], { type: mimeType }), filename)
}

/** 支持的文档类型 → 文件名中使用的类型名 */
export const DOC_TYPE_LABELS = {
  prd: 'PRD',
  'api-docs': '接口文档',
  prompts: '提示词套件',
} as const

/** 文档类型 */
export type DocType = keyof typeof DOC_TYPE_LABELS

/**
 * 去掉文件名中的非法字符。
 *
 * 覆盖 Windows 的 `\ / : * ? " < > |` 以及 C0 控制符：这些字符会让下载失败，
 * 或在落盘时被系统静默改写。用户填的产品名是自由文本，必须过滤。
 */
function sanitizeFilenamePart(value: string): string {
  return value.replace(/[\\/:*?"<>|\u0000-\u001f]/g, '').trim()
}

/**
 * 按「产品名称-文档类型」拼装下载文件名。
 *
 * 产品名缺失、或清洗后为空时退回「文档类型_日期」的形式，避免产出
 * `-PRD.md` 这种没有主名的文件；日期后缀也能避免多次下载互相覆盖。
 *
 * @param docType 文档类型
 * @param productName 产品名称，通常取结构化摘要的 `product_name`
 */
export function buildDocFilename(docType: DocType, productName?: string): string {
  const label = DOC_TYPE_LABELS[docType]
  const name = sanitizeFilenamePart(productName ?? '')
  if (name) return `${name}-${label}.md`

  const date = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const stamp = `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}`
  return `${label}_${stamp}.md`
}

/**
 * 交付包的文件名：`{产品名}-delivery.zip`。
 *
 * 产品名与单份下载共用同一套清洗（非法字符会让落盘失败），缺失时退回「交付包」，
 * 不产出 `-delivery.zip` 这种没有主名的文件。
 */
export function buildBundleFilename(productName?: string): string {
  const name = sanitizeFilenamePart(productName ?? '')
  return `${name || '交付包'}-delivery.zip`
}
