import { redactDiagnosticText } from './errorRecovery.js'

export function readableAppDiagnostic(error, limit = 6000) {
  const raw = redactDiagnosticText(error?.message || error || 'Unknown app error')
  return raw.length > limit
    ? `${raw.slice(0, limit)}\n[diagnostic truncated]`
    : raw
}

/** Format untrusted runtime output so a chat draft never presents it as prose. */
export function appDiagnosticBlock(error, limit = 6000) {
  return readableAppDiagnostic(error, limit)
    .split('\n')
    .map(line => `    ${line}`)
    .join('\n')
}
