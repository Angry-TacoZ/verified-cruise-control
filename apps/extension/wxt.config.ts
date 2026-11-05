import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'wxt';

// Build-time port overrides (VCC_API_HOST / VCC_FIXTURE_HOST) exist for local
// port conflicts only; defaults are the documented loopback origins.
const apiHost = process.env.VCC_API_HOST ?? 'http://127.0.0.1:8000/*';
const fixtureHost = process.env.VCC_FIXTURE_HOST ?? 'http://127.0.0.1:8901/*';
// Site-neutral (ADR 003): no listing-site hosts are pre-granted. Image
// CDN origins are deliberately not pre-granted either; the popup requests
// each origin at runtime through optional host permissions behind an
// explicit operator approval. Unpermitted hosts fail as recorded permission
// failures, never silent drops.

export default defineConfig({
  modules: ['@wxt-dev/module-svelte'],
  vite: () => ({
    plugins: [tailwindcss()],
  }),
  manifest: {
    name: 'Verified Cruise Control',
    description: 'Import vehicle listings and original photos into the local audit app.',
    // `scripting` + `activeTab`: temporary user-activated access to the
    // selected tab only. No content scripts auto-run on listing sites.
    permissions: ['storage', 'activeTab', 'scripting'],
    // Narrowly scoped: loopback Django API + synthetic fixture server used
    // for E2E tests. No listing-site hosts, no all-hosts wildcard, no
    // content-script CORS wildcard.
    host_permissions: [apiHost, fixtureHost],
    // Optional: unknown image CDN origins. Declared but never auto-granted —
    // each origin is requested via permissions.request() behind an explicit
    // operator action, and the operator can deny per site.
    optional_host_permissions: ['https://*/*', 'http://*/*'],
    icons: {
      16: 'icon-16.png',
      32: 'icon-32.png',
      48: 'icon-48.png',
      128: 'icon-128.png',
    },
  },
});
