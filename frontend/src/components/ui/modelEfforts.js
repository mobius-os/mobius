function labelForEffort(value) {
  return String(value)
    .replace(/[-_]+/g, ' ')
    .replace(/^./, (letter) => letter.toUpperCase())
}

/**
 * Resolves the effort scale for one registry model.
 *
 * Providers still supply the default scale, while an optional
 * `effort_levels` array on a model can narrow, reorder, or extend it. That
 * keeps today's registry backwards-compatible and gives future models a
 * declarative capability surface instead of adding model-id conditionals to
 * every picker.
 */
export function modelEfforts(providerEfforts, model) {
  const defaults = Array.isArray(providerEfforts) ? providerEfforts : []
  const levels = model?.effort_levels
  if (!Array.isArray(levels) || levels.length === 0) return defaults

  const known = new Map(defaults.map((effort) => [effort.value, effort]))
  const resolved = []
  const seen = new Set()
  for (const entry of levels) {
    const value = typeof entry === 'string' ? entry : entry?.value
    if (!value || seen.has(value)) continue
    seen.add(value)
    resolved.push(
      known.get(value) || {
        value,
        label: typeof entry === 'object' && entry?.label
          ? entry.label
          : labelForEffort(value),
      },
    )
  }
  return resolved.length ? resolved : defaults
}

export function validEffort(efforts, preferred) {
  const rows = Array.isArray(efforts) ? efforts : []
  if (rows.some((effort) => effort.value === preferred)) return preferred
  return rows.find((effort) => effort.value === 'medium')?.value
    || rows[0]?.value
    || ''
}
