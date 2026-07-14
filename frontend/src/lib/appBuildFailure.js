const FALLBACK_APP_NAME = 'app'
const REASSURANCE = 'The previous version is still running.'
const MAX_SUMMARY_LENGTH = 160

function compact(text) {
  return String(text || '').replace(/\s+/g, ' ').trim()
}

function truncate(text, limit) {
  if (text.length <= limit) return text
  return `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`
}

export function appBuildFailureAppName(event) {
  return compact(event?.appName || event?.name || '') || FALLBACK_APP_NAME
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
  const appName = appBuildFailureAppName(eventOrError)
  const summary = summarizeAppBuildFailure(eventOrError)
  if (!summary) return `Couldn't compile ${appName}. ${REASSURANCE}`
  return `Couldn't compile ${appName} — ${summary}. ${REASSURANCE}`
}
