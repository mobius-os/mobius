const FALLBACK_APP_NAME = 'this app'
const REASSURANCE = 'The previous version is still running.'
const MAX_SUMMARY_LENGTH = 160
const APP_STORE_MANIFEST_PREFIX = 'https://raw.githubusercontent.com/mobius-os/app-store/'
const TERMINAL_PUNCTUATION = /[.!?…]$/

function compact(text) {
  return String(text || '').replace(/\s+/g, ' ').trim()
}

function truncate(text, limit) {
  if (text.length <= limit) return text
  return `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`
}

function appName(event) {
  return compact(event?.appName || event?.name || '') || FALLBACK_APP_NAME
}

function sentence(text) {
  return TERMINAL_PUNCTUATION.test(text) ? text : `${text}.`
}

export function summarizeAppBuildFailure(eventOrError) {
  if (typeof eventOrError === 'string') {
    return truncate(compact(eventOrError), MAX_SUMMARY_LENGTH)
  }
  return truncate(
    compact(eventOrError?.summary || eventOrError?.error || ''),
    MAX_SUMMARY_LENGTH,
  )
}

export function appBuildFailureMessage(eventOrError) {
  const name = appName(eventOrError)
  const summary = summarizeAppBuildFailure(eventOrError)
  if (!summary) return `Couldn't compile ${name}. ${REASSURANCE}`
  return `Couldn't compile ${name} — ${sentence(summary)} ${REASSURANCE}`
}

export function appUpdateStaleMessage(event) {
  return `The pending update for ${appName(event)} changed upstream. `
    + `Review the latest update and start again. ${REASSURANCE}`
}

export function findAppStoreApp(apps) {
  let nameFallback = null
  for (const app of Array.isArray(apps) ? apps : []) {
    if (String(app?.manifest_url || '').startsWith(APP_STORE_MANIFEST_PREFIX)) {
      return app
    }
    if (nameFallback == null && app?.name === 'App Store') nameFallback = app
  }
  return nameFallback
}
