const FALLBACK_MESSAGE = 'Shell rebuild failed. Previous shell is still running.'
const MAX_SUMMARY_LENGTH = 160
const MAX_DETAILS_LENGTH = 8000

function rawError(eventOrError) {
  if (typeof eventOrError === 'string') return eventOrError
  if (eventOrError && typeof eventOrError.error === 'string') {
    return eventOrError.error
  }
  return ''
}

function compact(text) {
  return String(text || '').replace(/\s+/g, ' ').trim()
}

function truncate(text, limit) {
  if (text.length <= limit) return text
  return `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`
}

function isBuildNoise(line) {
  return /^(vite v|transforming|rendering chunks|computing gzip size|✓|✗ Build failed|PWA v|mode\s+|format:|precache\s+|files generated$)/i
    .test(line)
}

export function summarizeShellRebuildFailure(eventOrError) {
  const raw = rawError(eventOrError).trim()
  if (!raw) return ''

  const lines = raw
    .split(/\r?\n/)
    .map(compact)
    .filter(Boolean)

  const highSignal = lines.find(line => (
    /\bERROR:\s*/i.test(line)
    || /\bEACCES\b/i.test(line)
    || /\bFailed to resolve\b/i.test(line)
    || /\bCannot find module\b/i.test(line)
    || /\bUnexpected\b/.test(line)
    || /\bExpected\b/.test(line)
    || /\berror during build\b/i.test(line)
  ))

  let summary = highSignal || lines.find(line => !isBuildNoise(line)) || lines[0] || ''
  summary = summary.replace(/^.*?\bERROR:\s*/i, '')
  return truncate(summary, MAX_SUMMARY_LENGTH)
}

export function shellRebuildFailureMessage(eventOrError) {
  const summary = summarizeShellRebuildFailure(eventOrError)
  return summary ? `Shell rebuild failed: ${summary}` : FALLBACK_MESSAGE
}

export function shellRebuildFailureDetails(eventOrError) {
  const raw = rawError(eventOrError).trim()
  if (!raw) return ''
  return truncate(raw, MAX_DETAILS_LENGTH)
}
