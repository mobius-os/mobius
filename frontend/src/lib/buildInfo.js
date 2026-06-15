// Build/version marker for the shell bundle.
//
// Why this exists: the SHELL service worker precaches the Vite-hashed
// entry bundle by FILENAME (`assets/index-<hash>.js`, precache revision
// null — Workbox treats the content hash in the name AS the cache key).
// A returning installed PWA only refetches the entry when that filename
// changes. So a fix that lands ONLY in non-bundled surfaces — backend
// Python (theme.py) or the separately-precached app-frame.html — leaves
// `index-<hash>.js` byte-identical, the SW keeps serving the stale cached
// copy, and returning users never receive the new shell.
//
// `SHELL_BUILD` is a real constant tied to a specific shell change: bump
// it whenever a fix must reach already-installed PWAs but doesn't itself
// alter a bundled module. Because it's imported into the entry (main.jsx)
// and logged at startup, changing this string changes the entry bundle's
// content and forces Vite to emit a new `index-<hash>.js` filename — the
// new precache URL the SW needs to see to refetch. This is the honest,
// minimal lever for "rotate the bundle so every PWA refetches": no random
// strings, just a human-readable record of why the bundle moved.
export const SHELL_BUILD = '2026-06-15-light-mode-batch'
