export function projectTypeKind(value) {
  if (typeof value === 'string') return value || 'blank'
  if (!value || typeof value !== 'object') return 'blank'
  return value.kind || value.template?.kind || 'blank'
}

export function defaultProjectName(template) {
  const name = String(template?.name || '').trim()
  if (!name || template?.key === 'blank') return 'Untitled project'
  return `Untitled ${name.toLowerCase()}`
}

// Core templates and installed Project apps compose; names and glyph kinds do
// not decide which providers are allowed into the picker.
export function globalProjectTemplates(templates) {
  const rows = (Array.isArray(templates) ? templates : []).filter(t => !t.retired)
  return [...rows.filter(t => t.source_app_id == null), ...rows.filter(t => t.source_app_id != null)]
}

export function projectAppTemplates(templates) {
  return globalProjectTemplates(templates).filter(t => t.source_app_id != null)
}

export function normalizeProjectColor(value) {
  return typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value)
    ? value.toLowerCase()
    : null
}

export function projectIdentityTone(value) {
  const custom = normalizeProjectColor(
    value && typeof value === 'object' ? value.color : null,
  )
  return {
    kind: projectTypeKind(value),
    accent: custom || 'var(--text)',
  }
}
