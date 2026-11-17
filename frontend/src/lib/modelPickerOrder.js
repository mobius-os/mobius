/** Stable, cross-provider picker order. Catalog order breaks ties and remains
 *  authoritative for models the owner has never chosen. */
export function orderPickerModels(rows, recentIds = []) {
  const rank = new Map(recentIds.map((id, index) => [id, index]))
  return [...rows].sort((a, b) => (
    (rank.get(a.model.id) ?? Infinity) - (rank.get(b.model.id) ?? Infinity)
  ))
}

/** Cache recency only after a confirmed picker save, never on staged picks. */
export function withRecentModel(prefs, modelId, limit = 20) {
  return {
    ...prefs,
    recent_ids: [modelId, ...(prefs?.recent_ids || []).filter(id => id !== modelId)].slice(0, limit),
  }
}
