// The queued-tray caption must state the REAL reason a message is waiting.
// The prior copy ("Will send after the current turn finishes") was hardcoded and
// rendered even when no turn was active — e.g. a message queued while Möbius was
// restarting — which read as "stuck". Kept pure and separate so this contract is
// testable without a DOM, and so any future caller derives the same honest copy.
export function queuedHint({ turnActive, online = true, restarting = false, held = false }) {
  // Held (by Stop, or after a setup error): no turn, finishing or not, sends
  // them until the owner does.
  if (held) return 'Waiting for you — sends when you send'
  if (turnActive) return 'Will send after the current turn finishes'
  if (restarting || !online) return 'Will send when you reconnect'
  return 'Queued to send'
}
