// Bounded presentation for any manifest-declared app activity. Domain wording
// and resources belong to the app; the shell owns identity and safe navigation.

const SAFE_APP_SLUG = /^[a-z0-9][a-z0-9_-]{0,127}$/
const STATUSES = new Set(['running', 'succeeded', 'empty', 'failed'])

function cleanText(value, limit) {
  if (typeof value !== 'string') return ''
  return value.replace(/\s+/g, ' ').trim().slice(0, limit)
}

function resourceHref(appSlug, intent) {
  if (!SAFE_APP_SLUG.test(appSlug) || typeof intent !== 'string') return ''
  const safeIntent = intent.trim().slice(0, 512)
  if (!safeIntent || /[\u0000-\u001f]/.test(safeIntent)) return ''
  return `/shell/?app=${appSlug}&intent=${encodeURIComponent(safeIntent)}`
}

export function appActivityCardModel(activity) {
  if (!activity || typeof activity !== 'object' || !STATUSES.has(activity.status)) {
    return null
  }
  const appSlug = cleanText(activity.app_slug, 128)
  const appName = cleanText(activity.app_name, 120) || 'App'
  const label = cleanText(activity.label, 160) || `${appName} activity`
  const resources = []
  const seen = new Set()
  for (const [index, raw] of (Array.isArray(activity.resources)
    ? activity.resources : []).entries()) {
    const resourceLabel = cleanText(raw?.label, 160)
    const intent = typeof raw?.intent === 'string'
      ? raw.intent.trim().slice(0, 512) : ''
    const key = `${resourceLabel}\u0000${intent}`
    if (!resourceLabel || seen.has(key)) continue
    seen.add(key)
    resources.push({
      key: `${key}\u0000${index}`,
      label: resourceLabel,
      summary: cleanText(raw?.summary, 400),
      href: resourceHref(appSlug, intent),
    })
  }
  return {
    status: activity.status,
    appName,
    label,
    detail: cleanText(activity.detail, 600),
    warning: cleanText(activity.warning, 600),
    resources,
    receiptMissing: activity.receipt_missing === true,
  }
}

export function appActivityLabel(tool) {
  const model = appActivityCardModel(tool?.app_activity)
  if (!model) return tool?.status === 'running' ? 'Using an app' : 'Used an app'
  return `${model.appName}: ${model.label}`
}
