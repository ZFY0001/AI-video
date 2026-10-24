import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 后端默认 8000。若被占用，服务会自动往后找端口，此时在 web/.env 里写
// VITE_API_TARGET=http://127.0.0.1:8001 让开发服务器转发过去。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'VITE_')
  const target = env.VITE_API_TARGET || 'http://127.0.0.1:8000'

  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: 5173,
      strictPort: false,
      proxy: {
        '/api': { target, changeOrigin: false },
      },
    },
    build: {
      outDir: 'dist',
      // 不自动清空：这台机器上 dist 会被回收站删除器卡住（ETIMEDOUT），
      // 每次构建都失败。产物名带 hash，旧文件留着不影响加载。
      emptyOutDir: false,
      chunkSizeWarningLimit: 900,
    },
  }
})
