/* The discoverable keyboard-shortcut catalog is the single source of truth for
   shell shortcut matching and labels. */

export const SHELL_SHORTCUTS = {
  openSearch: {
    id: 'search.open',
    title: 'Open search',
    description: 'Search chats and installed apps from anywhere in Möbius.',
    scope: 'Anywhere',
    binding: { key: 'k', mod: true },
  },
  undoWorkspace: {
    id: 'workspace.undo',
    title: 'Undo workspace change',
    description: 'Undo the latest pane or tab arrangement outside text fields.',
    scope: 'Workspace',
    binding: { key: 'z', mod: true },
  },
  toggleBuilder: {
    id: 'workspace.toggle-builder',
    title: 'Toggle Builder mode',
    description: 'Switch workspace modes while the Möbius logo is focused.',
    scope: 'Möbius logo',
    binding: { key: 'Enter', shift: true },
  },
}

export const SHORTCUT_CATALOG = Object.values(SHELL_SHORTCUTS)

export function shortcutMatches(event, shortcut) {
  if (!event || !shortcut?.binding || event.isComposing || event.repeat) return false
  const binding = shortcut.binding
  const eventKey = typeof event.key === 'string' ? event.key.toLowerCase() : ''
  const bindingKey = String(binding.key || '').toLowerCase()
  if (!bindingKey || eventKey !== bindingKey) return false

  const hasMod = Boolean(event.metaKey || event.ctrlKey)
  if (hasMod !== Boolean(binding.mod)) return false
  if (Boolean(event.shiftKey) !== Boolean(binding.shift)) return false
  if (Boolean(event.altKey) !== Boolean(binding.alt)) return false
  return true
}

export function shortcutLabel(shortcut, platform = globalThis.navigator?.platform || '') {
  const binding = shortcut?.binding
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

// Future owner customization should persist only `{ id, binding }` overrides
// for catalogued actions. Never deserialize executable callbacks from app or
// owner data; apps keep their own iframe-local shortcuts unless the shell later
// introduces a reviewed, intent-based action declaration.
