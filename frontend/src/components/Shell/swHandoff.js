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

// Whether a freshly-mounted shell should re-arm its apply-on-idle reload because
// the page is NOT running the generation the service worker now serves. Any one
// of these means a newer shell generation exists that the page has not adopted;
// re-arming routes it back through the same hold-until-idle apply path.
//
//   - stalePrecacheFlagged: index.html's boot check saw the page's bundle differ
//     from network /sw.js (the Chromium stale-precache class) and flagged it.
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
// Pure over a plain snapshot (`waiting`/`active`/`controller` are opaque worker
// references compared by identity) so the decision is unit-testable without a
// live service worker.
export function shouldRearmShellApply({
  stalePrecacheFlagged = false,
  waiting = null,
  active = null,
  controller = null,
} = {}) {
  if (stalePrecacheFlagged) return true
  if (waiting) return true
  if (active && controller && controller !== active) return true
  return false
}

// Foreground/online shell-update watch — the APPLY trigger that lets a deploy
// reach an ALREADY-INSTALLED PWA promptly, closing the "still broken after the
// deploy" gap for a warm install.
//
// The gap: deploy-prod.sh broadcasts a `shell_rebuilt` system event, but that is
// a TRANSIENT push to currently-connected clients. A PWA that was BACKGROUNDED
// across the deploy has its EventSource suspended and the event is never replayed
// on reconnect, so it never learns a new bundle shipped. It also does not
// re-mount, so the boot re-arm net (shouldRearmShellApply at mount) cannot
// re-fire. index.html DOES call reg.update() on visibility, which installs the new
// worker — but under the SW update leash that worker INSTALLS AND WAITS, and
// index.html's watchdog only reloads on 'activated', which a leashed worker never
// reaches on its own. So the update is discovered but nothing applies it, and the
// page keeps serving the OLD bundle until a true cold start.
//
// This wires the missing apply at the owning layer (the apply-on-idle machine):
// on every return to visible (and on regaining connectivity) it forces a fresh
// sw.js fetch and, once a newer generation is waiting/mismatched, calls `rearm()`.
// The caller routes `rearm` to requestShellReload, which posts SKIP_WAITING to the
// waiting worker and reloads at the next IDLE boundary — silent (no toast), and
// deferred while a turn streams or the owner is typing, so the sacred stream is
// never cut. Gated by shouldRearmShellApply, so a return with no new generation is
// a no-op (never a spurious reload → no reload loop: after the apply the page runs
// the new generation, active === controller, nothing waits, decide() is false).
//
// Deps are injected (doc/win/serviceWorker/readStaleFlag/rearm) so the wiring is
// unit-testable without a live service worker. Returns a dispose function.
export function watchForShellUpdateOnForeground({
  doc,
  win,
  serviceWorker,
  readStaleFlag = () => false,
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

  const decide = (reg) => {
    if (disposed || applied || !reg) return
    if (shouldRearmShellApply({
      stalePrecacheFlagged: readStaleFlag(),
      waiting: reg.waiting || null,
      active: reg.active || null,
      controller: serviceWorker.controller || null,
    })) {
      applied = true
      rearm()
    }
  }

  const runCheck = async () => {
    let reg
    try { reg = await serviceWorker.getRegistration() } catch { return }
    if (disposed || !reg) return
    // Force a fresh sw.js fetch so a deploy that shipped while we were backgrounded
    // is discovered now (a cheap conditional GET; the server 304s when unchanged).
    try { await reg.update() } catch { /* offline / transient — decide on what we have */ }
    if (disposed) return
    // Settle on the NEWEST generation (review finding 2): update() resolves BEFORE
    // the newly-discovered worker finishes installing. If a newer generation is
    // still INSTALLING, defer the decision until it reaches installed/redundant so
    // we never apply reg.waiting (an OLDER generation) first and then reload again
    // into the newer one — one reload, into the newest. With nothing installing
    // (the common case: a worker installed while we were away and is already
    // waiting), decide now so the apply lands on this first foreground return.
    const installing = reg.installing
    const stillInstalling = installing
      && installing.state !== 'installed'
      && installing.state !== 'redundant'
      && typeof installing.addEventListener === 'function'
    if (stillInstalling) {
      const onState = () => {
        if (installing.state === 'installed' || installing.state === 'redundant') {
          installing.removeEventListener('statechange', onState)
          decide(reg)
        }
      }
      installing.addEventListener('statechange', onState)
    } else {
      decide(reg)
    }
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
  win?.addEventListener?.('online', check)
  return () => {
    disposed = true
    doc.removeEventListener('visibilitychange', onVisible)
    win?.removeEventListener?.('online', check)
  }
}
