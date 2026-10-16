import { mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import PastePairStage from '../components/stages/PastePairStage.vue'

/**
 * 双粘贴向导（「我有 PRD + 接口文档」）。
 *
 * 这一页的全部风险集中在**两份材料的地位不对称**上：PRD 必填、接口文档选填。写错了的
 * 后果都很隐蔽——把接口文档也当必填，用户会被一个「我确实没有」的框挡在门外；反过来
 * 把 PRD 当选填，就会拿着一份空材料去生成。因此这里逐条钉住校验、提交载荷与清空行为。
 *
 * 组件本身只往外抛事件（`generate` / `back` / 两个 `update:`），不发请求、不碰 store。
 */

const PRD = '# 小账本 产品需求文档（PRD）\n\n## 1. 产品概述\n家庭记账工具'
const API_DOCS = '# 小账本 接口文档\n\n## 记账\nPOST /api/ledgers'

/** 挂载录入页；两份材料的初始值由用例给 */
function mountStage(props: { prdText?: string; apiDocsText?: string } = {}): VueWrapper {
  return mount(PastePairStage, {
    props: {
      prdText: props.prdText ?? '',
      apiDocsText: props.apiDocsText ?? '',
    },
  })
}

/** 按文案取按钮（模板里文案都是纯文字，精确匹配即可） */
function buttonByText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text().trim() === text)
  if (!button) throw new Error(`未找到按钮：${text}`)
  return button
}

/** 在文本框里输入并同步 props（真实使用中值由 store 回灌） */
async function typeInto(
  wrapper: VueWrapper,
  selector: string,
  value: string,
  prop: 'prdText' | 'apiDocsText',
): Promise<void> {
  await wrapper.find(selector).setValue(value)
  await wrapper.setProps({ [prop]: value })
}

describe('PastePairStage 的录入与校验', () => {
  it('挂载时聚焦 PRD 框：它是本页唯一必填项', () => {
    // 必须真的挂进文档才能验焦点：不 attach 时元素不在 DOM 里，activeElement 恒为 body
    const wrapper = mount(PastePairStage, {
      props: { prdText: '', apiDocsText: '' },
      attachTo: document.body,
    })

    expect(document.activeElement).toBe(wrapper.find('#field-pair-prd').element)

    wrapper.unmount()
  })

  it('PRD 为空时主操作禁用，缺失提示常驻在字段下方', () => {
    const wrapper = mountStage()

    expect((buttonByText(wrapper, '生成提示词套件').element as HTMLButtonElement).disabled).toBe(
      true,
    )
    expect(wrapper.text()).toContain('请先贴入 PRD 正文')
  })

  it('只有 PRD 就能提交：接口文档留空不算缺件', async () => {
    const wrapper = mountStage()
    await typeInto(wrapper, '#field-pair-prd', PRD, 'prdText')

    const generate = buttonByText(wrapper, '生成提示词套件')
    expect((generate.element as HTMLButtonElement).disabled).toBe(false)

    await generate.trigger('click')
    expect(wrapper.emitted('generate')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('请先贴入 PRD 正文')
  })

  it('两份都填时提示词与接口文档的关系讲清楚了', async () => {
    const wrapper = mountStage({ prdText: PRD, apiDocsText: API_DOCS })

    expect(wrapper.text()).toContain('接口路径与字段名会与它保持一致')
  })

  it('接口文档留空时说明「按只有 PRD 生成」，并点出与另一条入口等价', () => {
    const wrapper = mountStage({ prdText: PRD })

    expect(wrapper.text()).toContain('留空即按「只有 PRD」生成')
  })

  it('两份材料的字数各自独立统计', async () => {
    const wrapper = mountStage({ prdText: PRD, apiDocsText: API_DOCS })

    expect(wrapper.text()).toContain(`${PRD.length} 字`)
    expect(wrapper.text()).toContain(`${API_DOCS.length} 字`)
  })

  it('输入即上报，两个字段各走各的事件', async () => {
    const wrapper = mountStage()

    await wrapper.find('#field-pair-prd').setValue('# PRD')
    await wrapper.find('#field-pair-api-docs').setValue('# 接口')

    expect(wrapper.emitted('update:prdText')).toEqual([['# PRD']])
    expect(wrapper.emitted('update:apiDocsText')).toEqual([['# 接口']])
  })

  it('PRD 不是 Markdown 时给提示但不拦提交', async () => {
    const wrapper = mountStage({ prdText: '一段没有标题的纯文本' })

    expect(wrapper.text()).toContain('没检测到 Markdown 标题')
    expect(
      (buttonByText(wrapper, '生成提示词套件').element as HTMLButtonElement).disabled,
    ).toBe(false)
  })

  it('「返回入口」抛出 back，本组件不自己跳转', async () => {
    const wrapper = mountStage()

    await buttonByText(wrapper, '返回入口').trigger('click')

    expect(wrapper.emitted('back')).toHaveLength(1)
  })
})

describe('PastePairStage 的确认条与清空', () => {
  it('确认条分别回执两份材料，缺的那份明写「未提供」', () => {
    const withDocs = mountStage({ prdText: PRD, apiDocsText: API_DOCS })
    expect(withDocs.text()).toContain('已录入：小账本 产品需求文档（PRD）')
    expect(withDocs.text()).toContain('接口文档：小账本 接口文档')

    const withoutDocs = mountStage({ prdText: PRD })
    expect(withoutDocs.text()).toContain('接口文档：未提供')
  })

  it('PRD 为空时不显示确认条（没有可回执的东西）', () => {
    const wrapper = mountStage({ apiDocsText: API_DOCS })

    expect(wrapper.text()).not.toContain('已录入：')
  })

  it('「清空」把两份材料一起清掉', async () => {
    const wrapper = mountStage({ prdText: PRD, apiDocsText: API_DOCS })

    await buttonByText(wrapper, '清空').trigger('click')

    expect(wrapper.emitted('update:prdText')).toEqual([['']])
    expect(wrapper.emitted('update:apiDocsText')).toEqual([['']])
  })

  it('两份都空时「清空」禁用（没有东西可清）', () => {
    const wrapper = mountStage()

    expect((buttonByText(wrapper, '清空').element as HTMLButtonElement).disabled).toBe(true)
  })

  it('只有接口文档时「清空」可用：那一份也是用户敲进来的', () => {
    const wrapper = mountStage({ apiDocsText: API_DOCS })

    expect((buttonByText(wrapper, '清空').element as HTMLButtonElement).disabled).toBe(false)
  })
})
