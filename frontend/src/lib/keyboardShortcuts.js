/* Shell shortcut bindings stay centralized so matching and labels cannot drift. */

export const SHELL_SHORTCUTS = {
  openSearch: { key: 'k', mod: true },
  undoWorkspace: { key: 'z', mod: true },
  toggleBuilder: { key: 'Enter', shift: true },
}

export function shortcutMatches(event, binding) {
  if (!event || !binding || event.isComposing || event.repeat) return false
  const eventKey = typeof event.key === 'string' ? event.key.toLowerCase() : ''
  const bindingKey = String(binding.key || '').toLowerCase()
  if (!bindingKey || eventKey !== bindingKey) return false

  const hasMod = Boolean(event.metaKey || event.ctrlKey)
  if (hasMod !== Boolean(binding.mod)) return false
  if (Boolean(event.shiftKey) !== Boolean(binding.shift)) return false
  if (Boolean(event.altKey) !== Boolean(binding.alt)) return false
  return true
}

export function shortcutLabel(binding, platform = globalThis.navigator?.platform || '') {
  if (!binding) return ''
  const apple = /Mac|iPhone|iPad|iPod/i.test(platform)
  const parts = []
  if (binding.mod) parts.push(apple ? '⌘' : 'Ctrl')
  if (binding.alt) parts.push(apple ? '⌥' : 'Alt')
  if (binding.shift) parts.push(apple ? '⇧' : 'Shift')
  const key = binding.key === 'Enter'
    ? (apple ? '↵' : 'Enter')
    : String(binding.key || '').toUpperCase()
  if (key) parts.push(key)
  return apple ? parts.join('') : parts.join('+')
}
