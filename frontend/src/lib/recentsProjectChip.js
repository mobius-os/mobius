// Pure helper: does a recents row carry a project chip, and with what payload?
//
// The chats-list backend now emits `row.project = { id, name, color } | null` (see the
// build spec's Recents fix). A chat that belongs to a project renders a small
// clickable chip beside its label that opens that project. This isolates the
// "is there a chip and what does it say" decision so the Drawer row stays a thin
// renderer and the rule is unit-testable.

// Returns { id, name, color } for the chip, or null when the row shows none. Chats and
// artifacts carry a project; an app row never does. A malformed/partial project
// object (missing id) yields no chip rather than a dead control.
export function recentsProjectChip(kind, item) {
  if (kind !== 'chat' && kind !== 'artifact') return null
  const project = item?.project
  if (!project || typeof project !== 'object') return null
  if (project.id == null || String(project.id).trim() === '') return null
  const name = typeof project.name === 'string' && project.name.trim()
    ? project.name.trim()
    : 'Project'
  const color = typeof project.color === 'string' && /^#[0-9a-f]{6}$/i.test(project.color)
    ? project.color.toLowerCase()
    : null
  return { id: String(project.id), name, color }
}
