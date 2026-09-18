import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发模式下 Vite 直接代理后端，因此 FastAPI 不需要开放任何 CORS。
// 交付模式下由 FastAPI 托管 dist/，base 必须是 /app/ 才能对上资源路径。
const BACKEND = 'http://127.0.0.1:8000'

const API_PREFIXES = [
  '/query',
  '/retrieval',
  '/graph',
  '/system',
  '/healthz',
  '/metrics',
  '/feedback',
  '/ingestions',
  '/ingestion-jobs',
  '/documents',
  '/models',
  '/model-connections',
  '/adapters',
  '/workspaces',
  '/extractions',
  '/candidate-entities',
  '/candidate-relations',
  '/candidates',
]

export default defineConfig({
  plugins: [react()],
  base: '/app/',
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      API_PREFIXES.map((prefix) => [prefix, { target: BACKEND, changeOrigin: true }]),
    ),
  },
})
