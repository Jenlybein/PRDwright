import './index.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'

import AppV2 from './AppV2.vue'

// V2 已定稿：V1（App.vue）与 `#v2` hash 切换一并移除，AppV2 成为唯一入口。
// 会话阶段机与会话数据由 Pinia store（stores/workflow.ts）承载。
createApp(AppV2).use(createPinia()).mount('#app')
