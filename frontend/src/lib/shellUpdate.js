// Service-worker apply-handoff helpers for the shell's apply-on-idle reload.
//
// Design context (sw.js "SW UPDATE LEASH", design §1.3): a new shell generation
// INSTALLS AND WAITS — it never skipWaiting()s on its own. The page hands it
// control at an idle apply boundary so the SW generation flips exactly when the
// page generation does. These helpers make that flip DETERMINISTIC and make a
// missed flip self-heal, closing feature 207: the first apply after a publish
// could reload before the new worker took over, land back on the OLD
// generation (the outgoing worker answered the navigation from its precache),
// and stick there with its one apply consumed until the next publish.

// Bounded fallback for the takeover wait. The waiting worker normally reaches
// 'activated' in well under this once it receives SKIP_WAITING; the timeout only
// covers a wedged install, where we reload anyway so an apply is never lost (the
// boot-time re-arm net then catches a stale landing).
export const SW_TAKEOVER_TIMEOUT_MS = 2000
export const SW_DISCOVERY_SETTLE_TIMEOUT_MS = 2000

// Force the registration to discover the newest shell generation and wait for
// that worker's install attempt to settle before choosing `registration.waiting`.
// `registration.update()` resolves after a newly-found worker enters
// `installing`, not after its install event settles. Choosing the waiting worker
// at that earlier instant can hand off to generation N while N+1 is still
// installing, producing a second immediate reload when N+1 becomes ready.
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

// Reload only once the waiting worker has actually taken over — not on a blind
// timer. The previous code posted SKIP_WAITING fire-and-forget and reloaded a
// fixed 220ms later; on a client's FIRST update cycle the waiting worker can be
// slow to spin up, so skipWaiting()->activate had not finished when the reload
// fired and the navigation was answered by the OUTGOING worker's precache — the
// page came back on the old generation and stuck (feature 207).
//
// We reload on the first of these signals:
//   - the waiting worker's state reaches 'activated' — it is now the
//     registration's active worker, so the reload navigation below adopts it as
//     controller (a fresh navigation takes the active worker even without
//     clients.claim()); this is the authoritative "new generation is live" cue
//     on a leashed update.
//   - a controllerchange fires — belt-and-suspenders; on a leashed update
//     without clients.claim() it usually does not, but if a claim ever happens
//     it is decisive.
//   - the waiting worker goes 'redundant' — superseded/failed; reload anyway and
//     let the boot-time re-arm net recover.
//   - the bounded timeout elapses — SW wedged mid-install; reload anyway.
// No waiting worker (unchanged sw.js — e.g. a backend-only rebuild) → reload
// now: the reload alone re-fetches the current generation.
//
// Dependencies (serviceWorker, timers, reload) are injected so the wiring is
// unit-testable without a live service worker.
export function reloadWhenWorkerTakesOver({
  registration,
  serviceWorker,
  reload,
  timeoutMs = SW_TAKEOVER_TIMEOUT_MS,
  setTimeoutFn = (typeof setTimeout !== 'undefined' ? setTimeout : null),
  clearTimeoutFn = (typeof clearTimeout !== 'undefined' ? clearTimeout : null),
} = {}) {
  const waiting = registration?.waiting
  if (!waiting) { reload(); return }

  let settled = false
  let timer = null
  const finish = () => {
    if (settled) return
    settled = true
    if (timer != null && clearTimeoutFn) clearTimeoutFn(timer)
    serviceWorker?.removeEventListener?.('controllerchange', onControllerChange)
    waiting.removeEventListener?.('statechange', onStateChange)
    reload()
  }
  const onControllerChange = () => finish()
  const onStateChange = () => {
    if (waiting.state === 'activated' || waiting.state === 'redundant') finish()
  }

  serviceWorker?.addEventListener?.('controllerchange', onControllerChange)
  waiting.addEventListener?.('statechange', onStateChange)
  if (setTimeoutFn) timer = setTimeoutFn(finish, timeoutMs)
  try { waiting.postMessage({ type: 'SKIP_WAITING' }) } catch { /* ignore */ }
  // The worker may already be past 'waiting' by the time we attached above.
  if (waiting.state === 'activated' || waiting.state === 'redundant') finish()
}

// Whether the page is NOT running the generation the service worker now serves.
// Either state means a newer shell generation exists that the page has not
// adopted; callers route it through the same hold-until-idle apply path.
//
//   - waiting: a newer worker installed and is WAITING (leashed) — its apply
//     signal was lost or has not fired yet.
//   - active !== controller: the registration has an ACTIVE worker that is not
//     the one controlling the page — feature 207's settled state, where the new
//     worker skipWaiting()'d to active but the page's reload was answered by the
//     outgoing worker's precache, so it sits on the old bundle with a
//     now-redundant controller. reg.waiting is null there, which a waiting-only
//     check cannot see; this identity comparison is what makes the 4-minute
//     stale state impossible to sit in.
//
// `waiting`/`active`/`controller` are opaque worker references compared by
// identity. This predicate stays private so callers cannot bypass inspection.
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
  await settleRegistrationUpdate({
    registration,
    timeoutMs,
    setTimeoutFn,
    clearTimeoutFn,
  })
  return {
    registration,
    updateAvailable: hasNewerShellGeneration({
      waiting: registration.waiting || null,
      active: registration.active || null,
      controller: serviceWorker.controller || null,
    }),
  }
}

// Error-recovery reload that ESCAPES a stale service-worker generation.
//
// The error boundary's refresh used a blind location.reload(). On an installed
// PWA a blind reload is answered by the CURRENTLY-CONTROLLING worker's precache,
// so a crash caused by a stale bundle reloads straight back into the same broken
// generation — recovery loops out to an agent instead of self-healing. Every
// other reload in the shell escapes this through reloadWhenWorkerTakesOver
// (performReload); this is the error-recovery analogue. It forces a fresh sw.js
// fetch (so a just-shipped worker is discovered), then hands control to a waiting
// worker before reloading, so recovery lands on the NEWEST generation.
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
  handoff = reloadWhenWorkerTakesOver,
} = {}) {
  const { registration, updateAvailable } = await inspectShellUpdate({ serviceWorker })
  if (!registration || !updateAvailable) return false
  handoff({ registration, serviceWorker, reload })
  return true
}

// Resume/online shell-update watch — the APPLY trigger that lets a deploy
// reach an ALREADY-INSTALLED PWA promptly, closing the "still broken after the
// deploy" gap for a warm install.
//
// The gap: deploy-prod.sh broadcasts a `shell_rebuilt` system event, but that is
// a TRANSIENT push to currently-connected clients. A PWA that was BACKGROUNDED
// across the deploy has its EventSource suspended and the event is never replayed
// on reconnect, so it never learns a new bundle shipped. It also does not
// re-mount, so the boot inspection cannot re-fire. Without this watcher nothing
// discovers and applies the waiting generation until a later navigation.
//
// This wires the missing apply at the owning layer (the apply-on-idle machine):
// on every return to the page (and on regaining connectivity) it forces a fresh
// sw.js fetch and, once a newer generation is waiting/mismatched, calls `rearm()`.
// A desktop app/window switch can leave document.visibilityState === 'visible',
// so visibilitychange alone is not a resume boundary. Window focus covers that
// path; pageshow covers a document restored from the back-forward cache.
// The caller routes `rearm` to requestShellReload, which posts SKIP_WAITING to the
// waiting worker and reloads at the next IDLE boundary — silent (no toast), and
// deferred while a turn streams or the owner is typing, so the sacred stream is
// never cut. Gated by the shared inspector, so a return with no new generation is
// a no-op (never a spurious reload → no reload loop: after the apply the page runs
// the new generation, active === controller, nothing waits, decide() is false).
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
  // Performing guard (review finding 1): rearm() hands off to requestShellReload,
  // which posts SKIP_WAITING + reloads. It is applied AT MOST ONCE per watch
  // lifetime — a near-simultaneous visibilitychange + online (or a waiting +
  // installed decision) must not fire two cache flushes / SW handoffs / reloads on
  // an immediately-applicable surface. Once requested, the page reloads; a second
  // request is only redundant/harmful.
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

  // Coalesce concurrent triggers (review finding 1): a near-simultaneous
  // visibilitychange + online must run ONE check, not two — otherwise each attaches
  // its own installing-statechange listener and both fire rearm. One shared promise
  // dedups overlapping runs; sequential returns after it clears run fresh (the
  // `applied` latch still bounds the eventual rearm to once).
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
