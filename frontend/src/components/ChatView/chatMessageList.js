/**
 * Structural equality for the small, rendered chat window.
 *
 * ChatView normally holds at most 20 durable rows. Comparing every row is
 * intentional: a terminal/reconnect fetch can finish an EARLIER assistant row
 * after a newer user row is already visible. A last-row-only shortcut calls
 * those snapshots equal and updates the query cache without updating mounted
 * React state, so the completed reply stays missing until a remount.
 *
 * False negatives are safe (one redundant render). False positives are not.
 */

function sameMessage(a, b) {
  if (a === b) return true
  if (!a || !b) return false
  // Messages are JSON-domain values by contract. Comparing their serialized
  // shape catches every current and future render-affecting field (tool output,
  // question options, attachments, pause metadata, hidden/optimistic flags)
  // instead of maintaining another field allowlist that can become stale.
  // Property-order drift can only create a safe false negative / extra render.
  return JSON.stringify(a) === JSON.stringify(b)
}

export function sameMessageList(a, b) {
  if (a === b) return true
  if (!Array.isArray(a) || !Array.isArray(b)) return false
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    if (!sameMessage(a[i], b[i])) return false
  }
  return true
}
