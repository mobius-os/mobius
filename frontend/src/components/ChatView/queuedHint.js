// The queued-tray caption must state the REAL reason a message is waiting.
// The prior copy ("Will send after the current turn finishes") was hardcoded and
// rendered even when no turn was active — e.g. a message queued while Möbius was
// restarting — which read as "stuck". Kept pure and separate so this contract is
// testable without a DOM, and so any future caller derives the same honest copy.
export function queuedHint({ turnActive, online = true, restarting = false, held = 0, total = held }) {
  // Held rows (after Stop, or a setup error) wait for the owner's send; no
  // turn, finishing or not, sends them. Rows queued since still drain as usual.
  if (held > 0 && held >= total) return 'Waiting for you — sends when you send'
  if (held > 0) {
    return turnActive
      ? `${held} waiting for you; the rest send after this turn`
      : `${held} waiting for you`
  }
  if (turnActive) return 'Will send after the current turn finishes'
  if (restarting || !online) return 'Will send when you reconnect'
  return 'Queued to send'
}
