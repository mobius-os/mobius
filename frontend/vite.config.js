import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'
import { webcrypto } from 'node:crypto'

if (!globalThis.crypto) {
  globalThis.crypto = webcrypto
}

// Service-worker integration uses `injectManifest` rather than the
// `generateSW` shortcut: the SW source at `src/sw.js` still has
// hand-written push + notification-click handlers that don't fit
// Workbox's stock recipes, so we keep ownership of the SW and let
// the plugin only INJECT the precache manifest (`self.__WB_MANIFEST`)
// into it. That replaces the previous hand-edited `VERSION = 'vN'`
// constant with build-content-hashed cache names — every Vite
// build produces a new precache identity, old caches get purged
// on activate by `cleanupOutdatedCaches`, no manual bumps.
export default defineConfig({
  plugins: [
    // Tailwind v4 — required by @openai/apps-sdk-ui so its
    // `@theme static {}` token blocks resolve. Without this plugin
    // SDK design tokens (--radius-full, --color-ring, etc.) parse
    // as unknown at-rules and silently produce empty values. Order
    // matters: tailwindcss before React so token transforms are
    // available to the @import "tailwindcss" + SDK CSS chain in
    // index.css.
    tailwindcss(),
    react(),
    VitePWA({
      srcDir: 'src',
      filename: 'sw.js',
      strategies: 'injectManifest',
      // We keep the manual `navigator.serviceWorker.register('/sw.js')`
      // call in `index.html` — the plugin's auto-register injection
      // would race with it on the cold path. Same SW URL, just
      // dual sources of truth would risk double-register.
      injectRegister: null,
      injectManifest: {
        // Precache the shell entry + the Vite-hashed bundle. Skip
        // the prebuilt mini-app frame HTML (`app-frame.html`) and
        // anything under `vendor/` — those are runtime-cached by
        // workbox-routing rules in sw.js, not precached, so they
        // don't bloat the install-time payload.
        globPatterns: ['**/*.{js,css,html,svg,png,ico,webmanifest}'],
        globIgnores: ['vendor/**', 'app-frame.html'],
        // ROOT FIX for stale installed PWAs: give EVERY precache
        // entry a real content-hash revision.
        //
        // vite-plugin-pwa defaults `dontCacheBustURLsMatching` to
        // `/^assets\//`. Workbox computes a content hash for every
        // file, then that default nulls the revision for anything
        // under `assets/` on the theory that "the Vite hash in the
        // filename IS the cache key, so a revision is redundant."
        // That theory holds ONLY while a content change always moves
        // the filename. It breaks the moment content changes WITHOUT
        // the filename moving — a rebuild that re-emits the same hash
        // (the change was already baked in earlier, or a chunk got
        // reused). Then the precache entry stays
        // `{revision:null,url:"assets/index-<hash>.js"}`, Workbox
        // sees no change, and the installed PWA serves stale code
        // forever. That is the "light mode still broken after the
        // server was fixed" failure, previously worked around by
        // bumping a SHELL_BUILD constant to force a filename move.
        //
        // Pointing `dontCacheBustURLsMatching` at a regex that can
        // never match a real precache URL disables the null-out, so
        // Workbox keeps the content hash it already computed for
        // every entry. Behavior:
        //   - content changes (any file) → new hash → SW refetches.
        //   - file unchanged → identical hash → no refetch (so a
        //     deploy does not needlessly re-download everything).
        //   - filename-hashed assets get the same hash whether keyed
        //     by name or by revision; the revision is now the
        //     authoritative busting signal regardless of the name.
        // \0 (NUL) cannot appear in a generated asset path, so this
        // matches nothing while staying a valid RegExp.
        dontCacheBustURLsMatching: /\0/,
        // Defensive: keep bundles within the cap so a future
        // big-dep bump fails the build loudly instead of silently
        // skipping precache.
        maximumFileSizeToCacheInBytes: 3 * 1024 * 1024,
      },
      // `registerType` is intentionally omitted. It only takes
      // effect when the plugin injects a client-side registration
      // helper, but we've set `injectRegister: null` (we register
      // the SW ourselves in `index.html`). Setting `registerType`
      // here would be misleading — a future reader might assume
      // the plugin manages the update lifecycle and remove the
      // manual `self.skipWaiting()` + `clientsClaim()` calls from
      // `src/sw.js` thinking they're redundant. Those calls are
      // load-bearing for auto-update with this strategy.

      // No manifest config here — `frontend/public/manifest.webmanifest`
      // is the source of truth and the server rewrites theme colors
      // on every request (see backend/app/theme.py). Letting the
      // plugin generate a separate manifest would create a second
      // source that immediately desyncs.
      manifest: false,
      devOptions: { enabled: false },
    }),
  ],
  server: {
    cors: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
