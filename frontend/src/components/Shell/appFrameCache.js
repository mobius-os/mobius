export const BASE_APP_CACHE_MAX = 6
export const HIGH_MEMORY_APP_CACHE_MAX = 10

/**
 * Keep the established six-frame budget unless the browser positively reports
 * at least 8 GB through the Device Memory API. Unknown devices (including
 * browsers that omit the API) must not take on additional iframe pressure.
 */
export function appFrameCacheMaxForDeviceMemory(deviceMemoryGb) {
  return Number.isFinite(deviceMemoryGb) && deviceMemoryGb >= 8
    ? HIGH_MEMORY_APP_CACHE_MAX
    : BASE_APP_CACHE_MAX
}

export function deriveRenderedAppIds({
  visibleAppIds,
  singleScreen,
  warmIds,
  max = BASE_APP_CACHE_MAX,
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
