/**
 * Device-independent cadence for progressively revealing assistant text.
 *
 * Three characters at a nominal 60Hz preserves the existing visual speed
 * (~180 chars/sec). Elapsed-time budgeting keeps that speed stable on 30Hz,
 * 90Hz, and 120Hz displays, while a small commit floor prevents high-refresh
 * panels from doubling React/Markdown work for no visible benefit.
 */

export const TEXT_REVEAL_CHARS_PER_SECOND = 180
export const TEXT_REVEAL_MIN_COMMIT_MS = 12
export const TEXT_REVEAL_MAX_ELAPSED_MS = 50

export function textRevealBudget({
  elapsedMs,
  carry = 0,
  bufferLength,
}) {
  const available = Math.max(0, Number(bufferLength) || 0)
  if (available === 0) return { count: 0, carry: 0 }

  const elapsed = Math.min(
    TEXT_REVEAL_MAX_ELAPSED_MS,
    Math.max(0, Number(elapsedMs) || 0),
  )
  const budget = Math.max(0, Number(carry) || 0)
    + elapsed * TEXT_REVEAL_CHARS_PER_SECOND / 1000
  const count = Math.min(available, Math.floor(budget + Number.EPSILON))
  const remaining = budget - count

  return {
    count,
    carry: count >= available || Math.abs(remaining) < 1e-9 ? 0 : remaining,
  }
}
