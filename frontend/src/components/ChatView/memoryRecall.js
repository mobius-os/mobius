// Safe presentation helpers for the Memory recall card. The backend stamps
// recall metadata on the one event-publishing funnel, but note IDs and app
// slugs still become navigation URLs here, so the browser validates them again
// rather than widening the generic web-source URL policy.

// Only a note id may reach a deep link. The backend already validates the
// path, but this value builds a URL that navigates the shell, so re-check here
// rather than trusting an upstream call site to stay correct forever.
const SAFE_NOTE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
const SAFE_MEMORY_APP_SLUG = /^memory(?:-[0-9]+)?$/

export function safeNoteId(value) {
  if (typeof value !== 'string') return ''
  const candidate = value.trim()
  if (!candidate || candidate.length > 128) return ''
  return SAFE_NOTE_ID.test(candidate) ? candidate : ''
}

export function safeMemoryAppSlug(value) {
  if (typeof value !== 'string') return ''
  const candidate = value.trim()
  return SAFE_MEMORY_APP_SLUG.test(candidate) ? candidate : ''
}

// Where a pill points: the Memory app, asked to open this note. `?app=<slug>&
// intent=<text>` is the shell's existing internal-nav contract (the same one
// artifact links use), so this adds no new navigation mechanism.
export function noteHref(note) {
  const id = safeNoteId(note?.id)
  if (!id) return ''
  // Old persisted citations predate app_slug and necessarily came from the
  // original unsuffixed install. New citations carry the slug validated from
  // the command path; a present-but-invalid value fails closed.
  const hasAppSlug = typeof note?.app_slug === 'string'
  const appSlug = hasAppSlug ? safeMemoryAppSlug(note.app_slug) : 'memory'
  return appSlug
    ? `/shell/?app=${appSlug}&intent=${encodeURIComponent(`note:${id}`)}`
    : ''
}

// Titles come from the Memory app's own graph. A lookup whose titled section
// lines were carved out of a large tool output still yields a readable label
// from the note id, so a result row is never blank.
export function noteLabel(note) {
  const title = typeof note?.title === 'string' ? note.title.trim() : ''
  if (title) return title
  const id = safeNoteId(note?.id)
  return id ? id.replace(/[-_]+/g, ' ') : ''
}
