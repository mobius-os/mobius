// Discovery and lifecycle helpers for leashed shell service-worker updates.
// Online document freshness belongs to the navigation route in sw.js; this
// module only discovers a new worker, releases it at a safe boundary, and asks
// the shell's existing apply-on-idle controller for a reload.
export const SW_DISCOVERY_SETTLE_TIMEOUT_MS = 2000

// Force the registration to discover the newest shell generation and wait for
// that worker's install attempt to settle before choosing `registration.waiting`.
// `registration.update()` resolves after a newly-found worker enters
// `installing`, not after its install event settles. Choosing the waiting worker
// at that earlier instant can release generation N while N+1 is still
// installing, producing a second avoidable reload when N+1 becomes ready.
//
// The wait is bounded: a wedged install must not strand the page. On timeout the
// caller proceeds with the best currently-usable registration state and the
// boot re-arm net can recover a later generation.
async function settleRegistrationUpdate({
  registration,
  timeoutMs = SW_DISCOVERY_SETTLE_TIMEOUT_MS,
  setTimeoutFn = (typeof setTimeout !== 'undefined' ? setTimeout : null),
  clearTimeoutFn = (typeof clearTimeout !== 'undefined' ? clearTimeout : null),
} = {}) {
  if (!registration) return registration
  let installing = registration.installing
  const onUpdateFound = () => {
    installing = registration.installing || installing
  }
  registration.addEventListener?.('updatefound', onUpdateFound)
  if (typeof registration.update === 'function') {
    try { await registration.update() } catch { /* offline/transient — use current state */ }
  }

  installing = registration.installing || installing
  // The specification queues the registration object's `installing` update and
  // `updatefound` event before resolving update(), so allow that queued task to
  // publish the worker before deciding that no install exists.
  if (!installing && setTimeoutFn) {
    await new Promise(resolve => setTimeoutFn(resolve, 0))
    installing = registration.installing || installing
  }
  registration.removeEventListener?.('updatefound', onUpdateFound)
  const isSettled = () => (
    !installing
    || installing.state === 'installed'
    || installing.state === 'redundant'
  )
  if (isSettled() || typeof installing.addEventListener !== 'function') {
    return registration
  }

  await new Promise(resolve => {
    let finished = false
    let timer = null
    const finish = () => {
      if (finished) return
      finished = true
      if (timer != null && clearTimeoutFn) clearTimeoutFn(timer)
      installing.removeEventListener?.('statechange', onStateChange)
      resolve()
    }
    const onStateChange = () => { if (isSettled()) finish() }
    installing.addEventListener('statechange', onStateChange)
    if (setTimeoutFn) timer = setTimeoutFn(finish, timeoutMs)
    // The worker can settle between the state read and listener attachment.
    if (isSettled()) finish()
  })
  return registration
}

// A waiting worker or an active/controller mismatch means this document
// predates the registration's newest generation. Worker references are opaque
// and intentionally compared by identity.
function hasNewerShellGeneration({
  waiting = null,
  active = null,
  controller = null,
} = {}) {
  if (waiting) return true
  if (active && controller && controller !== active) return true
  return false
}

// The single discovery boundary for every shell-update caller. It refreshes
// the registration, waits through the browser's queued updatefound/install
// lifecycle, and returns one authoritative decision with the registration that
// decision belongs to. Boot, foreground return, error recovery, and explicit
// apply must not each reconstruct a partial version of this sequence.
export async function inspectShellUpdate({
  serviceWorker,
  timeoutMs = SW_DISCOVERY_SETTLE_TIMEOUT_MS,
  setTimeoutFn = (typeof setTimeout !== 'undefined' ? setTimeout : null),
  clearTimeoutFn = (typeof clearTimeout !== 'undefined' ? clearTimeout : null),
} = {}) {
  if (!serviceWorker || typeof serviceWorker.getRegistration !== 'function') {
    return { registration: null, updateAvailable: false }
  }
  let registration = null
  try { registration = await serviceWorker.getRegistration() } catch { /* unavailable */ }
  if (!registration) return { registration: null, updateAvailable: false }
  // Preserve evidence that can disappear while update() settles. Another tab
  // may release the waiting worker and make the final registration look current
  // even though this document still needs one navigation.
  const waitingAtStart = registration.waiting || null
  const controllerAtStart = serviceWorker.controller || null
  await settleRegistrationUpdate({
    registration,
    timeoutMs,
    setTimeoutFn,
    clearTimeoutFn,
  })
  return {
    registration,
    updateAvailable: !!waitingAtStart
      || (!!controllerAtStart && serviceWorker.controller !== controllerAtStart)
      || hasNewerShellGeneration({
        waiting: registration.waiting || null,
        active: registration.active || null,
        controller: serviceWorker.controller || null,
      }),
  }
}

// Let a discovered worker activate, without coupling document freshness to the
// timing of that activation. Online shell navigation independently fetches the
// current document; this message exists only so the worker lifecycle can settle
// and its coherent offline precache can become current.
export function releaseWaitingShellUpdate(registration) {
  try { registration?.waiting?.postMessage({ type: 'SKIP_WAITING' }) } catch { /* unavailable */ }
}

// Error recovery reloads only when worker inspection proves this document is
// stale. Network-first shell navigation makes the reload itself ordinary.
//
// Reloads ONLY when inspection finds a newer generation; returns whether it did.
// The auto-heal caller keeps a
// false as "genuine bug on the newest build → show the recovery panel, never
// reload-loop"; the manual-refresh caller treats a false as "already newest →
// honor the refresh with a plain reload". Deps are injected so the wiring is
// unit-testable without a live worker.
export async function reloadIfGenerationStale({
  serviceWorker,
  reload,
} = {}) {
  const { registration, updateAvailable } = await inspectShellUpdate({ serviceWorker })
  if (!updateAvailable) return false
  releaseWaitingShellUpdate(registration)
  reload()
  return true
}

// A backgrounded PWA can miss the transient shell_rebuilt event. Check again on
// each real browser-return boundary and route a discovered generation through
// the shell's existing safe-reload policy. Focus covers desktop app switching,
// pageshow covers BFCache restoration, and online covers connectivity recovery.
//
// Deps are injected (doc/win/serviceWorker/rearm) so the wiring is
// unit-testable without a live service worker. Returns a dispose function.
export function watchForShellUpdateOnResume({
  doc,
  win,
  serviceWorker,
  rearm,
} = {}) {
  if (!doc || !serviceWorker || typeof serviceWorker.getRegistration !== 'function') {
    return () => {}
  }
  let disposed = false
  // A reload request is one-shot for this document. Coalescing below also keeps
  // simultaneous visibility/online signals on one inspection.
  let applied = false

  const decide = (updateAvailable) => {
    if (disposed || applied) return
    if (updateAvailable) {
      applied = true
      rearm()
    }
  }

  const runCheck = async () => {
    const { updateAvailable } = await inspectShellUpdate({ serviceWorker })
    if (disposed) return
    decide(updateAvailable)
  }

  let inFlight = null
  const check = () => {
    if (disposed || applied) return inFlight
    if (inFlight) return inFlight
    inFlight = runCheck().finally(() => { inFlight = null })
    return inFlight
  }

  const onVisible = () => { if (doc.visibilityState === 'visible') check() }
  doc.addEventListener('visibilitychange', onVisible)
  win?.addEventListener?.('focus', onVisible)
  win?.addEventListener?.('pageshow', onVisible)
  win?.addEventListener?.('online', check)
  return () => {
    disposed = true
    doc.removeEventListener('visibilitychange', onVisible)
    win?.removeEventListener?.('focus', onVisible)
    win?.removeEventListener?.('pageshow', onVisible)
    win?.removeEventListener?.('online', check)
  }
}
