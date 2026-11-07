// The queued-tray caption must state the REAL reason a message is waiting.
// The prior copy ("Will send after the current turn finishes") was hardcoded and
// rendered even when no turn was active — e.g. a message queued while Möbius was
// restarting — which read as "stuck". Kept pure and separate so this contract is
// testable without a DOM, and so any future caller derives the same honest copy.
export function queuedHint({
  turnActive, online = true, restarting = false, held = 0, total = held, sharedGuest = false,
}) {
  // Held rows (after Stop, or a setup error) wait for the owner's send; no
  // turn, finishing or not, sends them. Rows queued since still drain as usual.
  // A shared-browser guest sees the same state, but the owner is the one who
  // sends them.
  if (held > 0 && held >= total) {
    return sharedGuest ? 'Waiting for the owner to send' : 'Waiting for you — sends when you send'
  }
  if (held > 0) {
    const waiting = `${held} waiting for ${sharedGuest ? 'the owner' : 'you'}`
    return turnActive ? `${waiting}; the rest send after this turn` : waiting
  }
  if (turnActive) return 'Will send after the current turn finishes'
  if (restarting || !online) return 'Will send when you reconnect'
  return 'Queued to send'
}
