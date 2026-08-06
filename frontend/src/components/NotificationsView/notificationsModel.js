// Pure presentation helpers for the notification preview, split out of the JSX
// so `node --test` covers them without a DOM.

// The relative-time formatter is shared shell-wide; re-exported here so the
// notification preview's existing imports (and tests) stay put.
export { formatRelativeTime } from '../../lib/relativeTime.js'

// Row icon selection is keyed by source_type — a SERVER-controlled field —
// never by the row's `icon` URL, which an app-scoped sender writes free-form
// and therefore is untrusted (trust pre-flight §1). Unknown slugs get the
// default so a new producer degrades gracefully.
export const SOURCE_TYPE_ICONS = Object.freeze({
  system: 'system',
  agent: 'agent',
  chat: 'chat',
  app: 'app',
  platform_conflict: 'system',
})

export function iconKindForSource(sourceType) {
  return SOURCE_TYPE_ICONS[sourceType] ?? 'default'
}
