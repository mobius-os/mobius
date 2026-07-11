// Pure helpers for the Settings "Check for updates" control. The check fans out
// two independent probes — a service-worker cache re-check and a platform git
// fetch — via Promise.allSettled, which never rejects. So the component can't
// tell a clean "nothing new" from a check that actually failed unless it
// inspects the settled results. Keeping that decision here (side-effect-free, no
// React) makes the failure-honesty rule unit-testable in isolation, mirroring
// platformUpdatePreview.js / restartReadiness.js.

// Given the settled results of the update probes, decide the resting phase:
// 'error' if any probe rejected — be honest, don't claim "no updates" when the
// check itself failed — otherwise 'checked' (the transient success confirmation).
// allSettled never rejects, so a non-array is treated as no failures rather than
// masking a bug behind an error label.
export function updateCheckOutcome(results) {
  const settled = Array.isArray(results) ? results : []
  const anyFailed = settled.some((result) => result && result.status === 'rejected')
  return anyFailed ? 'error' : 'checked'
}

// The button's label for each update phase. 'error' persists (unlike the
// auto-resetting 'checked') so a failed check stays visible until the owner
// clicks again — the button doubles as the retry affordance.
export function updateCheckLabel(phase) {
  switch (phase) {
    case 'checking':
      return 'Checking…'
    case 'checked':
      return 'No updates found'
    case 'error':
      return "Couldn't check for updates"
    default:
      return 'Check for updates'
  }
}
