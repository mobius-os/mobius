/** Stable, cross-provider picker order. Catalog order breaks ties and remains
 *  authoritative for models the owner has never used. */
export function orderPickerModels(rows, recentIds = []) {
  const rank = new Map(recentIds.map((id, index) => [id, index]))
  return [...rows].sort((a, b) => (
    (rank.get(a.model.id) ?? Infinity) - (rank.get(b.model.id) ?? Infinity)
  ))
}
