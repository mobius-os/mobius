/* One-shot guard for the click a browser sends after a gesture that already
   did its job: a touch hold that opened a menu, or a finger drag that scrolled
   the Apps row. Only that gesture's own follow-up click is swallowed. Any
   fresh pointer or key intent retires the claim first, so a deliberate tap or
   key press afterwards always acts, even when the original gesture ended
   without a click (for example after pointercancel). */

export function createHeldClickGuard() {
  let claimed = false
  return {
    // The current gesture has acted; its follow-up click must not.
    claim() { claimed = true },
    // A new gesture or key press began; it owns its own click.
    retire() { claimed = false },
    // On click: true when this click belongs to the claimed gesture.
    consume() {
      const swallow = claimed
      claimed = false
      return swallow
    },
  }
}
