export const APP_CACHE_MAX = 6

export function deriveRenderedAppIds({
  visibleAppIds,
  singleScreen,
  warmIds,
  max = APP_CACHE_MAX,
}) {
  const result = new Set()
  for (const id of visibleAppIds) result.add(String(id))
  if (singleScreen?.kind === 'app') result.add(String(singleScreen.id))
  for (const id of warmIds) {
    if (result.size >= max) break
    result.add(String(id))
  }
  return [...result].sort((a, b) => Number(a) - Number(b))
}
