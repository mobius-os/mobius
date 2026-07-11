// Pure helpers for the drawer's "new app arrived" dot. A freshly built or
// installed app lands at the bottom of the oldest-first unpinned list with no
// affordance; Shell flags ids that appear AFTER a per-session baseline and the
// drawer renders a subtle accent dot until the app is first opened. Mirrors the
// attentionChatIds mechanism.
//
// Shell owns the stateful pieces (the baseline ref, the flagged-id Set); these
// functions hold the set arithmetic so the semantics are testable without
// rendering the shell. Everything coerces to Number so a string id from a
// restored route can't shadow the numeric id from the apps query.

// Ids in the current apps list that the session has not accounted for yet —
// genuine arrivals since the baseline was captured.
export function freshAppIds(accountedFor, currentIds) {
  const seen = accountedFor instanceof Set ? accountedFor : new Set(accountedFor)
  const out = []
  for (const raw of currentIds || []) {
    const id = Number(raw)
    if (!Number.isNaN(id) && !seen.has(id)) out.push(id)
  }
  return out
}

// Flag ids immutably, returning the SAME set when nothing changed so React can
// bail out of a re-render (mirrors Shell's attentionChatIds setters).
export function withAppsFlagged(prev, ids) {
  if (!ids || ids.length === 0) return prev
  let changed = false
  const next = new Set(prev)
  for (const raw of ids) {
    const id = Number(raw)
    if (!Number.isNaN(id) && !next.has(id)) {
      next.add(id)
      changed = true
    }
  }
  return changed ? next : prev
}

// Clear one flag when its app is opened. Same-reference return on a no-op.
export function withoutAppFlagged(prev, id) {
  const n = Number(id)
  if (Number.isNaN(n) || !prev.has(n)) return prev
  const next = new Set(prev)
  next.delete(n)
  return next
}
