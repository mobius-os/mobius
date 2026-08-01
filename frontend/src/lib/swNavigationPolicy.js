// Legacy instance-local mounts remain an extension point for installations
// that predate the reserved /services namespace. Stock Möbius ships none.
export const PROXIED_APP_SUBTREES = []

const SHELL_NAVIGATION_DENYLIST = [
  /^\/api(\/|$)/,
  /^\/app-assets\//,
  /^\/app-embeds\//,
  /^\/apps\//,
  /^\/recover(\/|$)/,
  /^\/shell\/embed(\/|$)/,
  // The push worker's scope (public/sw-push.js). It names a URL prefix inside
  // the shell's PWA scope and must never resolve to a document: a page there
  // would be controlled by that worker, which has no fetch handler, so it
  // would boot the shell with no precache and no offline fallback.
  /^\/shell\/push(\/|$)/,
  /^\/sites(\/|$)/,
  /^\/services(\/|$)/,
  ...PROXIED_APP_SUBTREES,
  /^\/(?!(?:shell|apps|recover)(?:\/|$))[A-Za-z0-9_-]+(?:\/(?:index\.html)?)?$/,
]

export function isShellNavigationDenied(pathname) {
  return SHELL_NAVIGATION_DENYLIST.some(pattern => pattern.test(pathname))
}
