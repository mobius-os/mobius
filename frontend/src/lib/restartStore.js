// Tracks whether Möbius is mid-restart, so the shell can show the SAME pulsing
// accent dot it already uses for reachability (Shell.css .shell__connection-status).
//
// A restart is three phases from the client's point of view:
//   1. the OLD process publishes `server_restarting` on the system bus right
//      before it drains — received while the client is still connected, which is
//      the ONLY signal that covers a graceful drain (health still answers, so
//      reachability alone would show nothing);
//   2. a transport gap while the process is down — here reachability's own
//      CHECKING/OFFLINE state takes over;
//   3. the system stream reconnects to the new process.
//
// Clearing is therefore belt-and-suspenders: the indicator clears on the first
// successful system-stream reconnect (Shell.reconcileSystemStateOnOpen) and —
// so a missed clear can never strand the dot — an UNCONDITIONAL max-duration
// auto-expire. The shell renders ONE dot from
// (reachabilityLabel || restartPending), so the drain shows this signal and the
// following process-down window shows reachability; there is never a second dot,
// and if a boot outlasts the auto-expire the dot stays lit via reachability
// (OFFLINE) rather than falsely clearing.

export const RESTART_INDICATOR_MAX_MS = 35_000

let pending = false
let expireTimer = null
const subscribers = new Set()

function notify() {
  for (const cb of subscribers) cb()
}

function stopTimer() {
  if (expireTimer !== null) {
    clearTimeout(expireTimer)
    expireTimer = null
  }
}

export function getRestartPendingSnapshot() {
  return pending
}

export function subscribeRestart(cb) {
  subscribers.add(cb)
  return () => { subscribers.delete(cb) }
}

export function setRestartPending() {
  // Always (re)arm the unconditional backstop, even when already pending: a
  // fresh `server_restarting` extends the window, and the timer is what
  // guarantees the dot can never stick if every clear signal is missed.
  stopTimer()
  expireTimer = setTimeout(clearRestartPending, RESTART_INDICATOR_MAX_MS)
  if (pending) return
  pending = true
  notify()
}

export function clearRestartPending() {
  stopTimer()
  if (!pending) return
  pending = false
  notify()
}

// Test seam: drop timer + subscribers between cases without reaching into the
// module's private bindings.
export function resetRestartStoreForTests() {
  stopTimer()
  pending = false
  subscribers.clear()
}
