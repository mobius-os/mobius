/*
 * Shell keyboard commands have one declarative catalog. The shell dispatcher,
 * command palette, app-frame bridge, and labels all consume this same data
 * rather than growing parallel key listeners.
 */

const DEFAULT_BINDINGS = Object.freeze({
  openSearch: Object.freeze({ key: 'k', code: 'KeyK', mod: true }),
  newChat: Object.freeze({ key: 'n', code: 'KeyN', mod: true }),
  newTab: Object.freeze({ key: 't', code: 'KeyT', mod: true }),
  closeTab: Object.freeze({ key: 'w', code: 'KeyW', mod: true }),
  reopenClosed: Object.freeze({ key: 't', code: 'KeyT', mod: true, shift: true }),
  newPane: Object.freeze({ key: '\\', code: 'Backslash', mod: true }),
  closePane: Object.freeze({ key: 'w', code: 'KeyW', mod: true, shift: true }),
  historyBack: Object.freeze({ key: ',', code: 'Comma', mod: true }),
  historyForward: Object.freeze({ key: '.', code: 'Period', mod: true }),
  undoWorkspace: Object.freeze({ key: 'z', code: 'KeyZ', mod: true }),
  toggleBuilder: Object.freeze({ key: 'Enter', code: 'Enter', shift: true }),
})

// Compatibility names for focused, non-general gestures that still have their
// own interaction owner (workspace undo and the logo's Builder toggle).
export const SHELL_SHORTCUTS = Object.freeze({
  openSearch: DEFAULT_BINDINGS.openSearch,
  undoWorkspace: DEFAULT_BINDINGS.undoWorkspace,
  toggleBuilder: DEFAULT_BINDINGS.toggleBuilder,
})

export const SHELL_COMMAND_DEFINITIONS = Object.freeze([
  {
    id: 'search.open',
    title: 'Search and commands',
    description: 'Find chats, apps, and workspace actions.',
    category: 'Workspace',
    keywords: ['command palette', 'find', 'open'],
    bindings: [DEFAULT_BINDINGS.openSearch],
    captureInMiniApps: true,
  },
  {
    id: 'chat.new',
    title: 'New chat',
    description: 'Start a new chat in the current workspace.',
    category: 'Chats',
    keywords: ['compose'],
    bindings: [DEFAULT_BINDINGS.newChat],
    captureInMiniApps: true,
  },
  {
    id: 'tab.newChat',
    title: 'New chat tab',
    description: 'Open a new chat as a Builder tab.',
    category: 'Tabs and panes',
    keywords: ['new tab', 'open tab'],
    bindings: [DEFAULT_BINDINGS.newTab],
    captureInMiniApps: true,
  },
  {
    id: 'tab.close',
    title: 'Close active tab',
    description: 'Close the active Builder tab.',
    category: 'Tabs and panes',
    keywords: ['remove tab'],
    bindings: [DEFAULT_BINDINGS.closeTab],
    captureInMiniApps: true,
  },
  {
    id: 'workspace.reopenClosed',
    title: 'Reopen last closed',
    description: 'Restore the most recently closed tab or pane.',
    category: 'Tabs and panes',
    keywords: ['undo close', 'restore tab', 'restore pane'],
    bindings: [DEFAULT_BINDINGS.reopenClosed],
    captureInMiniApps: true,
  },
  {
    id: 'pane.newChat',
    title: 'New chat pane',
    description: 'Open a new chat beside the focused pane.',
    category: 'Tabs and panes',
    keywords: ['split pane', 'open pane'],
    bindings: [DEFAULT_BINDINGS.newPane],
    captureInMiniApps: true,
  },
  {
    id: 'pane.close',
    title: 'Close focused pane',
    description: 'Close the focused pane and all of its tabs.',
    category: 'Tabs and panes',
    keywords: ['remove pane'],
    bindings: [DEFAULT_BINDINGS.closePane],
    captureInMiniApps: true,
  },
  {
    id: 'history.back',
    title: 'Go back',
    description: 'Return to the previous workspace destination.',
    category: 'Navigation',
    keywords: ['history previous'],
    bindings: [DEFAULT_BINDINGS.historyBack],
    captureInMiniApps: true,
  },
  {
    id: 'history.forward',
    title: 'Go forward',
    description: 'Move forward through workspace history.',
    category: 'Navigation',
    keywords: ['history next'],
    bindings: [DEFAULT_BINDINGS.historyForward],
    captureInMiniApps: true,
  },
])

export function normalizeShortcutBinding(binding) {
  if (!binding || typeof binding !== 'object') return null
  const key = typeof binding.key === 'string' ? binding.key.trim() : ''
  if (!key || key.length > 32) return null
  const code = typeof binding.code === 'string' && binding.code.length <= 32
    ? binding.code.trim()
    : ''
  return {
    key,
    ...(code ? { code } : {}),
    mod: binding.mod === true,
    shift: binding.shift === true,
    alt: binding.alt === true,
  }
}

export function resolveShellCommands() {
  return SHELL_COMMAND_DEFINITIONS.map(definition => ({
    ...definition,
    bindings: definition.bindings.map(normalizeShortcutBinding).filter(Boolean),
  }))
}

export function shortcutMatches(event, binding) {
  if (!event || !binding || event.isComposing || event.repeat) return false
  const eventKey = typeof event.key === 'string' ? event.key.toLocaleLowerCase() : ''
  const bindingKey = String(binding.key || '').toLocaleLowerCase()
  if (!bindingKey || eventKey !== bindingKey) return false

  const hasMod = Boolean(event.metaKey || event.ctrlKey)
  if (hasMod !== Boolean(binding.mod)) return false
  if (Boolean(event.shiftKey) !== Boolean(binding.shift)) return false
  if (Boolean(event.altKey) !== Boolean(binding.alt)) return false
  return true
}

export function findShellShortcut(event, commands) {
  for (const command of Array.isArray(commands) ? commands : []) {
    if (command.bindings?.some(binding => shortcutMatches(event, binding))) return command
  }
  return null
}

export function frameShortcutBindings(commands) {
  return (Array.isArray(commands) ? commands : []).flatMap(command => (
    command.captureInMiniApps
      ? command.bindings.map(binding => ({ actionId: command.id, binding }))
      : []
  ))
}

export function shortcutLockCodes(commands) {
  return [...new Set(frameShortcutBindings(commands).map(({ binding }) => (
    binding.code || null
  )).filter(Boolean))]
}

function displayKey(key, apple) {
  if (key === 'Enter') return apple ? '↵' : 'Enter'
  if (key === '\\') return '\\'
  if (key === ',') return ','
  if (key === '.') return '.'
  return String(key || '').toLocaleUpperCase()
}

export function shortcutLabel(binding, platform = globalThis.navigator?.platform || '') {
  if (!binding) return ''
  const apple = /Mac|iPhone|iPad|iPod/i.test(platform)
  const parts = []
  if (binding.mod) parts.push(apple ? '⌘' : 'Ctrl')
  if (binding.alt) parts.push(apple ? '⌥' : 'Alt')
  if (binding.shift) parts.push(apple ? '⇧' : 'Shift')
  const key = displayKey(binding.key, apple)
  if (key) parts.push(key)
  return apple ? parts.join('') : parts.join('+')
}
