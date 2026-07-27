// One canonical URL builder for installed-app artwork wherever the shell names
// an app, so the drawer, launcher, and tab strip cannot drift on cache-busting.
// This is the raw stored artwork (including alpha), not the standalone-PWA icon
// renderer, which may add an install-friendly background around that artwork.
export function appIconUrl(app, size = 128) {
  if (!app?.id || !app.has_custom_icon) return null
  const version = app.updated_at ? `&v=${encodeURIComponent(app.updated_at)}` : ''
  return `/api/apps/${encodeURIComponent(app.id)}/icon?size=${size}${version}`
}
