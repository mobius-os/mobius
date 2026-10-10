/* Keep disconnected providers visible but outside the reorderable priority. */

export function connectedProvidersFirst(rows, configuredProviders) {
  const connected = []
  const disconnected = []
  for (const row of rows) {
    if (configuredProviders.has(row.provider)) connected.push(row)
    else disconnected.push(row)
  }
  return [...connected, ...disconnected]
}

export function hasActiveConnectedProvider(rows, configuredProviders) {
  return rows.some(row => configuredProviders.has(row.provider) && row.enabled !== false)
}

export function moveConnectedProvider(rows, fromIndex, toIndex, configuredProviders) {
  const ordered = connectedProvidersFirst(rows, configuredProviders)
  const connectedCount = ordered.filter(row => configuredProviders.has(row.provider)).length
  if (
    fromIndex < 0 || fromIndex >= connectedCount
    || toIndex < 0 || toIndex >= connectedCount
    || fromIndex === toIndex
  ) return null
  const [row] = ordered.splice(fromIndex, 1)
  ordered.splice(toIndex, 0, row)
  return ordered
}
