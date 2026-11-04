import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'wxt';

// Build-time port overrides (VCC_API_HOST / VCC_FIXTURE_HOST) exist for local
// port conflicts only; defaults are the documented loopback origins.
const apiHost = process.env.VCC_API_HOST ?? 'http://127.0.0.1:8000/*';
const fixtureHost = process.env.VCC_FIXTURE_HOST ?? 'http://127.0.0.1:8901/*';
// First real listing host (ADR 002). Narrowly scoped: the site's VDP
// origins only. Image CDN origins are deliberately not pre-granted;
// unpermitted hosts fail as recorded permission failures, never silent drops.
const autotraderHosts = ['https://www.autotrader.com/*', 'https://autotrader.com/*'];

export default defineConfig({
  modules: ['@wxt-dev/module-svelte'],
  vite: () => ({
    plugins: [tailwindcss()],
  }),
  manifest: {
    name: 'Verified Cruise Control',
    description: 'Import vehicle listings and original photos into the local audit app.',
    permissions: ['storage', 'activeTab'],
    // Narrowly scoped: loopback Django API + synthetic fixture server used
    // for E2E tests + the first real listing host (ADR 002). No all-hosts
    // wildcard, no content-script CORS wildcard.
    host_permissions: [apiHost, fixtureHost, ...autotraderHosts],
    icons: {
      16: 'icon-16.png',
      32: 'icon-32.png',
      48: 'icon-48.png',
      128: 'icon-128.png',
    },
  },
});
