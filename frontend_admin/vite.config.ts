import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// 管理员端工程。与 `frontend/`（用户端）**完全独立**：各有一份依赖与构建产物，
// 谁也不会因为另一个的改动而构建失败。
//
// 端口固定 5174：用户端是 5173。固定而非让 Vite 自动顺延，是因为后端
// `cors_origins` 白名单里写死了来源——顺延到别的端口，请求会被 CORS 拦掉，
// 表现为「登录接口一直失败」而实际原因在端口上。
export default defineConfig({
  plugins: [tailwindcss(), vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      // /api 转发的后端与用户端**是同一个**：会话 Cookie 由后端设在 API 域上，
      // 两个前端只要调同一个源，登录态天然通用，不需要任何桥接。
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
