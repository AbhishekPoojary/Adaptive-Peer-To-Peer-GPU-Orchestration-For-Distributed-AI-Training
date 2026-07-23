import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// Dev-only proxy to the orchestrator API (http://localhost:8090), mounted
// under /api and rewritten to strip that prefix before forwarding. This
// keeps the dashboard a same-origin fetch from the browser's point of view
// so we never need CORS middleware on the orchestrator for local dev.
//
// IMPORTANT: this must NOT proxy bare prefixes like /nodes or /jobs directly
// — those exact strings are also this SPA's own client-side route paths
// (/nodes, /jobs). An earlier version of this config did that and broke
// "every page is refresh-safe": a hard navigation/refresh to /nodes was
// swallowed by the dev-server proxy and returned raw backend JSON instead of
// the app shell. Routing everything through the single /api prefix (which no
// page route uses) avoids the collision entirely.
const ORCHESTRATOR_URL = 'http://localhost:8090'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: ORCHESTRATOR_URL,
        changeOrigin: true,
        rewrite: (requestPath) => requestPath.replace(/^\/api/, ''),
      },
    },
  },
})
