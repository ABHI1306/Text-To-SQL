import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const mockApiPort = Number(env.VITE_MOCK_API_PORT ?? 4010)
  const apiTarget = mode === 'mock'
    ? `http://127.0.0.1:${mockApiPort}`
    : env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000'
  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: Number(env.VITE_DEV_PORT ?? 4173),
      proxy: {
        '/api/v1': apiTarget,
      },
    },
  }
})
