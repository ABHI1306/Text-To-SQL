import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const apiTarget = mode === 'mock'
    ? 'http://127.0.0.1:4010'
    : env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000'
  return {
    plugins: [react(), tailwindcss()],
    server: {
      proxy: {
        '/api/v1': apiTarget,
      },
    },
  }
})
