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
  // 生产挂在 `/admin/` 子路径下（ADR-054）：产物里的 JS / CSS / favicon 全部带这个前缀。
  //
  // ⚠️ **dev 也用同一个 base**，不做「开发用 /、生产用 /admin/」的双轨。
  // 代价是本地地址变成 `localhost:5174/admin/`；换来的是「挂子路径」这件事在本地就能验，
  // 而不是等到部署当天才发现某处依赖了站点根。双轨最典型的坑是：
  // 某个跳转/资源路径写死了 `/xxx`，dev 下看着正常，上线后全部 404——而那时是最贵的。
  base: '/admin/',
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
