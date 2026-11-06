import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * 后端地址。
 *
 * 默认是本机 8010；**容器里必须换成服务名**（`http://api:8010`）——
 * 容器内的 127.0.0.1 指的是它自己，不是后端。
 * 所以这里允许用环境变量覆盖，docker-compose.yml 里的 web 服务就是这么用的。
 *
 * 默认值走 `127.0.0.1` 而不是 `localhost`：Windows 上 `localhost` 可能先解析到
 * IPv6 的 `::1`，而 uvicorn 默认只监 IPv4，表现是代理偶发 502。
 */
// ⚠ 默认后端端口用 8010 而非 8000：本机另一个股票系统的 FastAPI/WebUI 默认占 8000。
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8010'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    // 用 Vite 的代理把 /api 转到后端，前端代码里写相对路径就行。
    // 好处：浏览器同源，完全不涉及 CORS；后端换端口也只改这一处。
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
      },
      '/health': {
        target: API_TARGET,
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
