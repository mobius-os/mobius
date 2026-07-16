/**
 * Owns the setup wizard's resume state.
 *
 * Two facts merged into one module:
 *   1. `setup-step` — localStorage key written by SetupWizard mid-flow
 *      so a refresh between account creation and the final onDone
 *      resumes at the right step instead of dropping the user into
 *      a Shell with no AI configured.
 *   2. `_inProgress` — flag the apiFetch interceptor reads to skip
 *      the 401 → clearToken → reload bounce during the wizard-to-
 *      shell transition (the setup endpoint legitimately races with
 *      a 401 if the new token is still in flight).
 *
 * Why module-not-hook: `_inProgress` must survive 401 responses and
 * possible redirects during the wizard-to-shell transition. That
 * means it can't live in React state — it has to be readable from
 * a plain `fetch` interceptor running outside any render cycle.
 *
 * Persistence: `_inProgress` mirrors to sessionStorage (per design
 * Open Q #2 resolution). Reload during setup is more common than
 * hard crash; Chrome's mobile tab-discard restores via sessionStorage.
 * The module-level `let` stays the synchronous source of truth for
 * same-tick reads; sessionStorage is the cross-reload backing store.
 *
 * HMR / test caveat: `_inProgress` survives Vite HMR and Vitest
 * module isolation. Tests MUST call `setInProgress(false)` in
 * afterEach to reset state between cases.
 */

const SETUP_STEP_KEY = 'setup-step'
const IN_PROGRESS_KEY = 'mobius-setup-in-progress'

// Safari ITP / private-browsing modes throw on storage access. Wrap
// every storage call in try/catch so an early-init read can't crash
// the whole bundle.
let _inProgress = (() => {
  try { return sessionStorage.getItem(IN_PROGRESS_KEY) === '1' }
  catch { return false }
})()

export function getResumeStep() {
  try {
    const v = localStorage.getItem(SETUP_STEP_KEY)
    if (v === 'provider') return v
    if (v !== null) localStorage.removeItem(SETUP_STEP_KEY)
    return null
  } catch { return null }
}

export function saveStep(step) {
  // Only persist past 'account' — that step has no token yet so
  // there's nothing meaningful to resume to.
  if (step === 'account') return
  try { localStorage.setItem(SETUP_STEP_KEY, step) } catch {}
}

export function clearResumeStep() {
  try { localStorage.removeItem(SETUP_STEP_KEY) } catch {}
}

export function setInProgress(value) {
  _inProgress = !!value
  try {
    if (value) sessionStorage.setItem(IN_PROGRESS_KEY, '1')
    else sessionStorage.removeItem(IN_PROGRESS_KEY)
  } catch {}
}

export function isInProgress() { return _inProgress }
