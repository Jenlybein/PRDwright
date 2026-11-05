import './index.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'

import AppV2 from './AppV2.vue'
import { router } from './router'

// V2 已定稿：V1（App.vue）与 `#v2` hash 切换一并移除，AppV2 成为唯一入口。
// 会话阶段机与会话数据由 Pinia store（stores/workflow.ts）承载；
// 页面级导航（新建 / 打开某份方案）交给 vue-router，两者职责不重叠。
const app = createApp(AppV2)

/**
 * 最外层兜底：**任何情况下都不能是整页白屏**。
 *
 * AppV2 的 `onErrorCaptured` 只管得住后代组件；它自己 setup 阶段抛的错会一路冒到这里
 * （典型来源：入口登记表缺行 / 缺列，问询当场失败——这是故意的失败路径）。
 * 这时界面本可能一片空白，因此补一句人看得懂的说明；已经渲染出内容的界面保持原样，
 * 不拿一句提示把用户正在看的东西盖掉。
 */
app.config.errorHandler = (error) => {
  console.error('[PRDwright] 应用启动失败', error)
  const root = document.getElementById('app')
  if (root !== null && root.childElementCount === 0) {
    root.textContent = '这块界面暂时不可用，请刷新页面重试。'
  }
}

app.use(createPinia()).use(router).mount('#app')
