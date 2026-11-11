// Captions for a waiting owner message must state the REAL reason it waits.
// The prior copy ("Will send after the current turn finishes") was hardcoded and
// rendered even when no turn was active — e.g. a message queued while Möbius was
// restarting — which read as "stuck". Kept pure and separate so this contract is
// testable without a DOM, and so any future caller derives the same honest copy.
export function queuedHint({ turnActive, online = true, restarting = false }) {
  if (turnActive) return 'Will send after the current turn finishes'
  if (restarting || !online) return 'Will send when you reconnect'
  return 'Queued to send'
}

// A steer accepted mid-turn is already delivered: it renders inline below the
// live output and the provider reads it at its next step boundary, so new work
// keeps appearing above it until then. Without a caption it reads as ignored;
// tray copy about sending would be wrong, since nothing is left to send.
export function steerPendingHint() {
  return 'Agent will read this after its current step'
}
