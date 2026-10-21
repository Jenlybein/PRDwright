import './index.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from '@/App.vue'
import { router } from '@/router'

// ⚠️ 注册顺序：Pinia 必须在 router **之前**。
// 路由守卫里要用 `useSessionStore()`，而 store 只有在 app 装上 Pinia 之后才有
// 「活动的 pinia 实例」；装晚了守卫会抛 "getActivePinia() was called but there
// was no active Pinia"。
createApp(App).use(createPinia()).use(router).mount('#app')
