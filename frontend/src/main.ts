import './index.css'

import { createApp } from 'vue'

import App from './App.vue'
import AppV2 from './AppV2.vue'

// V2 结构化录入表单入口：URL hash 为 #v2 时挂载 AppV2（固定格式 GUI 表单 +
// 解析函数自动转结构化 JSON），否则沿用 V1 流程版。功能开发期的临时切换方式，
// V2 定稿后再移除 V1。
createApp(window.location.hash === '#v2' ? AppV2 : App).mount('#app')
