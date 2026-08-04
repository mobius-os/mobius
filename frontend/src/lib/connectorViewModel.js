export function connectorSchemaCostLabel(estTokens) {
  if (!estTokens) return ''
  const value = estTokens >= 1000
    ? `${(estTokens / 1000).toFixed(estTokens >= 10000 ? 0 : 1)}k`
    : String(estTokens)
  return `~${value} tool-schema tokens`
}

export function connectorStatus(connection = {}) {
  if (connection.status === 'error') {
    return { color: '--danger', text: 'Needs attention' }
  }
  if (!connection.enabled) return { color: '--border', text: 'Off' }
  return { color: '--green', text: 'Available' }
}
