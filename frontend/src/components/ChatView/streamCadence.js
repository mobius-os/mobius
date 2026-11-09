/**
 * Device-independent cadence for progressively revealing assistant text.
 *
 * Start at ~180 chars/sec, then ease toward measured delivery throughput and
 * pending backlog. Elapsed-time budgeting is independent of display refresh
 * rate; a small commit floor prevents high-refresh panels from doubling
 * React/Markdown work for no visible benefit.
 *
 * Delayed frames must retain their elapsed credit. A cap lets rendering delay
 * grow the buffer while denying the revealer the time needed to catch up,
 * creating a self-reinforcing slowdown in long answers.
 */

export const TEXT_REVEAL_CHARS_PER_SECOND = 180
export const TEXT_REVEAL_MIN_COMMIT_MS = 12

export function textRevealBudget({
  elapsedMs,
  carry = 0,
  bufferLength,
  charsPerSecond = TEXT_REVEAL_CHARS_PER_SECOND,
}) {
  const available = Math.max(0, Number(bufferLength) || 0)
  if (available === 0) return { count: 0, carry: 0 }

  const elapsed = Math.max(0, Number(elapsedMs) || 0)
  const budget = Math.max(0, Number(carry) || 0)
    + elapsed * charsPerSecond / 1000
  const count = Math.min(available, Math.floor(budget + Number.EPSILON))
  const remaining = budget - count

  return {
    count,
    carry: count >= available || Math.abs(remaining) < 1e-9 ? 0 : remaining,
  }
}

/** Track delivery throughput separately from the eased, backlog-aware reveal speed. */
export class TextRevealPacer {
  constructor() {
    this.speed = TEXT_REVEAL_CHARS_PER_SECOND
    this.arrivalRate = TEXT_REVEAL_CHARS_PER_SECOND
    this.received = 0
    this.sampleAt = null
  }

  receive(count, now) {
    if (this.sampleAt === null) this.sampleAt = now
    this.received += count
  }

  rate(bufferLength, elapsedMs, now) {
    const sampleMs = now - this.sampleAt
    if (this.sampleAt !== null && sampleMs >= 120) {
      const weight = 1 - Math.exp(-sampleMs / 300)
      this.arrivalRate += (this.received * 1000 / sampleMs - this.arrivalRate) * weight
      this.received = 0
      this.sampleAt = now
    }
    // Keep a small cushion against bursty delivery, then catch up over half a
    // second. Ease speed changes instead of exposing transport chunk boundaries.
    const target = Math.max(45,
      this.arrivalRate + (bufferLength - this.arrivalRate * 0.12) / 0.5)
    this.speed += (target - this.speed) * (1 - Math.exp(-elapsedMs / 200))
    return this.speed
  }
}
