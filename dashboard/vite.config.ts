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
    // Vite rejects requests whose Host header it does not recognise, which
    // blocks a tunnel or LAN hostname outright. VITE_ALLOWED_HOSTS is a
    // comma-separated list for letting a remote collaborator reach this dev
    // server — e.g. so someone with no GPU can sign in and submit jobs to the
    // fleet, which is the whole point of the project.
    //
    // Opt-in by env rather than a permissive default: this is a *development*
    // server, and one left listening to the world is a bigger surface than the
    // orchestrator it fronts. For anything long-lived, build the dashboard and
    // serve the static output behind a real web server instead.
    allowedHosts: process.env.VITE_ALLOWED_HOSTS
      ? process.env.VITE_ALLOWED_HOSTS.split(',').map((h) => h.trim())
      : undefined,
    proxy: {
      '/api': {
        // Proxied server-side, so a remote browser never needs to reach the
        // orchestrator directly and no CORS configuration is involved.
        target: ORCHESTRATOR_URL,
        changeOrigin: true,
        rewrite: (requestPath) => requestPath.replace(/^\/api/, ''),
      },
    },
  },
})
