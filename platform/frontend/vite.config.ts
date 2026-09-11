import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 用 Vite 的代理把 /api 转到后端，前端代码里写相对路径就行。
    // 好处：浏览器同源，完全不涉及 CORS；后端换端口也只改这一处。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      output: {
        // 把两个大头（React Flow / ECharts）单独拆包，
        // 业务代码改动时这两个 chunk 的 hash 不变，浏览器缓存能复用
        manualChunks: {
          flow: ['@xyflow/react'],
          charts: ['echarts', 'echarts-for-react'],
        },
      },
    },
  },
})
