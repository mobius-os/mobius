import { FALLBACK_MODEL_GROUPS, PROVIDER_ORDER } from './constants.js'

function buildModelGroups(payload) {
  if (!payload || typeof payload !== 'object') return FALLBACK_MODEL_GROUPS
  const groups = []
  for (const meta of PROVIDER_ORDER) {
    const rows = Array.isArray(payload[meta.key]) ? payload[meta.key] : null
    if (!rows || rows.length === 0) continue
    groups.push({
      key: meta.key,
      label: meta.label,
      models: rows
        .filter((row) => row && typeof row.id === 'string')
        .map((row) => ({ id: row.id, name: row.name || row.id })),
    })
  }
  return groups
}

export async function fetchModelConfig(token) {
  const headers = { Authorization: `Bearer ${token}` }
  const [statusRes, modelsRes] = await Promise.all([
    fetch('/api/auth/providers/status', { headers }).catch(() => null),
    fetch('/api/auth/providers/models', { headers }).catch(() => null),
  ])
  let connected = null
  if (statusRes?.ok) {
    const data = await statusRes.json()
    connected = new Set(
      Object.entries(data || {})
        .filter(([, value]) => value && value.authenticated)
        .map(([key]) => key),
    )
  }
  const models = modelsRes?.ok ? buildModelGroups(await modelsRes.json()) : FALLBACK_MODEL_GROUPS
  return { connected, models }
}

// ---------------------------------------------------------------------------
