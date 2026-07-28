// AppOut owns icon existence, identity, and cache version. Callers only select
// the bounded rendition they need; they never reconstruct or probe the asset.
export function appIconUrl(app, size = 128) {
  if (!app?.icon_url) return null
  if (size == null) return app.icon_url
  const separator = app.icon_url.includes('?') ? '&' : '?'
  return `${app.icon_url}${separator}size=${size}`
}
