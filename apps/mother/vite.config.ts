import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vitest/config';

// Port recovery: fixed ports keep the extension manifest and proxy in
// agreement, but VCC_API_PROXY overrides the backend target when 8000 is
// occupied (see README "Port conflicts").
const apiTarget = process.env.VCC_API_PROXY ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [tailwindcss(), svelte()],
  // Under Vitest, force the `browser` export condition so `svelte` resolves
  // to index-client.js (default would be the server build, where mount()
  // throws). App builds keep Vite's default conditions.
  resolve: process.env.VITEST ? { conditions: ['browser'] } : undefined,
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      // Same-origin API access: the browser talks only to Vite, which
      // forwards /api to the loopback Django backend. No CORS involved.
      '/api': {
        target: apiTarget,
        changeOrigin: false,
      },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
  },
});
