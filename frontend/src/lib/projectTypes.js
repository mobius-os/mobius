function semanticTypeKey(value) {
  const raw = String(value || '').toLowerCase()
  const separator = raw.indexOf(':')
  if (separator < 0) return raw
  const provider = raw.slice(0, separator)
  const type = raw.slice(separator + 1)
  // LaTeX is itself the defining kind; other prefixes identify the provider
  // and must not leak words such as "web" into the template classification.
  return provider === 'latex' ? `latex ${type}` : type
}

function projectTypeWords(value) {
  if (!value) return ''
  if (typeof value === 'string') return semanticTypeKey(value)
  return [
    semanticTypeKey(value.project_type),
    semanticTypeKey(value.key),
    value.name,
    semanticTypeKey(value.template?.key),
    value.template?.name,
  ].filter(Boolean).join(' ').toLowerCase()
}

export function projectTypeKind(value) {
  const declared = value && typeof value === 'object' ? (value.kind || value.template?.kind) : ''
  if (declared) return declared
  const words = projectTypeWords(value)
  if (/github|repository|\brepo\b/.test(words)) return 'github'
  if (/latex|\.tex\b|paper/.test(words)) return 'latex'
  if (/visuali[sz]ation|chart|dashboard|data story/.test(words)) return 'visualization'
  if (/mini.?app|gadget|interactive app/.test(words)) return 'mini-app'
  if (/slides?|presentation|deck/.test(words)) return 'slides'
  if (/web|site|html/.test(words)) return 'web'
  if (/sheet|table|csv/.test(words)) return 'sheet'
  if (/document|docs|markdown|writing/.test(words)) return 'document'
  return 'blank'
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
