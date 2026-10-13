import { describe, expect, it } from 'vitest'

import { buildDocFilename, DOC_TYPE_LABELS } from '@/utils/download'

/**
 * 下载文件命名规则测试。
 *
 * 需求要求文件名形如「产品名称-文档类型」；产品名来自用户自由输入，
 * 因此必须验证非法字符过滤与缺失时的兜底，避免产出无主名文件。
 */
describe('buildDocFilename', () => {
  it('按「产品名称-文档类型」命名', () => {
    expect(buildDocFilename('prd', 'PRDwright')).toBe('PRDwright-PRD.md')
    expect(buildDocFilename('api-docs', 'PRDwright')).toBe('PRDwright-接口文档.md')
    expect(buildDocFilename('prompts', 'PRDwright')).toBe('PRDwright-提示词套件.md')
  })

  it('过滤产品名里的路径与保留字符', () => {
    expect(buildDocFilename('prd', 'A/B:C*D?E"F<G>H|I')).toBe('ABCDEFGHI-PRD.md')
    expect(buildDocFilename('prd', 'a\\b')).toBe('ab-PRD.md')
  })

  it('产品名缺失时退回「文档类型_日期」', () => {
    expect(buildDocFilename('prd', undefined)).toMatch(/^PRD_\d{8}\.md$/)
  })

  it('产品名只有空白时同样退回', () => {
    expect(buildDocFilename('prompts', '   ')).toMatch(/^提示词套件_\d{8}\.md$/)
  })

  it('产品名被清洗为空时不产出无主名文件', () => {
    expect(buildDocFilename('prd', '///')).toMatch(/^PRD_\d{8}\.md$/)
  })

  it('类型名与文档类型一一对应', () => {
    expect(Object.keys(DOC_TYPE_LABELS)).toEqual(['prd', 'api-docs', 'prompts'])
  })
})
