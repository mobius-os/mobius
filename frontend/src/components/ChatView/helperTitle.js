/* The one name the owner reads for a helper: its title, else its humanized task key. */

// "fix-chat-ui-glitches" -> "Fix chat ui glitches", matching the server's
// fallback (delegations.humanize_task_key) for helpers without a title.
export function humanizeTaskKey(key) {
  const words = String(key || '').replace(/[-_.]+/g, ' ').trim().replace(/\s+/g, ' ')
  return words ? words[0].toUpperCase() + words.slice(1) : ''
}

export function helperTitle(helper) {
  const title = typeof helper?.title === 'string' ? helper.title.trim() : ''
  return title || humanizeTaskKey(helper?.task_key) || 'Helper'
}
