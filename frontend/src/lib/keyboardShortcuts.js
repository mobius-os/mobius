/*
 * Shell keyboard commands have one declarative catalog. The shell dispatcher,
 * command palette, app-frame bridge, labels, reference UI, and owner overrides
 * all consume this same data rather than growing parallel key listeners.
 */

export const SHORTCUT_OVERRIDES_STORAGE_KEY = 'mobius:shell-shortcuts:v1'
export const SHORTCUT_OVERRIDES_CHANGED_EVENT = 'mobius:shell-shortcuts-changed'

const DEFAULT_BINDINGS = Object.freeze({
  openSearch: Object.freeze({ key: 'k', code: 'KeyK', mod: true }),
  openShortcutHelp: Object.freeze({ key: '/', code: 'Slash', mod: true }),
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
  openShortcutHelp: DEFAULT_BINDINGS.openShortcutHelp,
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
    id: 'shortcuts.open',
    title: 'Keyboard shortcuts',
    description: 'Show the shell keyboard reference.',
    category: 'Workspace',
    keywords: ['keys', 'help', 'commands'],
    bindings: [DEFAULT_BINDINGS.openShortcutHelp],
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
    // Chromium maps Cmd+, to Settings. Back is a shell command even when
    // there is no destination to traverse, so never release this chord to the
    // browser's unrelated command.
    reserveWhenUnavailable: true,
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

function browserStorage() {
  try { return globalThis.localStorage || null } catch { return null }
}

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

function normalizedOverrides(value) {
  const source = value?.actions && typeof value.actions === 'object'
    ? value.actions
    : {}
  const actions = {}
  for (const [id, override] of Object.entries(source)) {
    if (!/^[a-z][a-z0-9.-]{1,79}$/.test(id) || !override || typeof override !== 'object') continue
    const bindings = Array.isArray(override.bindings)
      ? override.bindings
        .map(normalizeShortcutBinding)
        // Shell commands are global, including inside app frames. Requiring
        // Cmd/Ctrl prevents an override from stealing ordinary text input.
        .filter(binding => binding?.mod)
        .slice(0, 8)
      : null
    actions[id] = {
      disabled: override.disabled === true,
      ...(bindings ? { bindings } : {}),
    }
  }
  return { version: 1, actions }
}

export function readShortcutOverrides(storage = browserStorage()) {
  try {
    return normalizedOverrides(JSON.parse(
      storage?.getItem(SHORTCUT_OVERRIDES_STORAGE_KEY) || '{}',
    ))
  } catch {
    return normalizedOverrides(null)
  }
}

// This narrow write seam lets an owner-facing app change shortcuts without
// coupling that app to shell rendering. Unknown action ids remain inert but
// survive platform version skew until the corresponding action is available.
export function writeShortcutOverrides(value, {
  storage = browserStorage(),
  target = typeof window !== 'undefined' ? window : null,
} = {}) {
  const normalized = normalizedOverrides(value)
  if (!storage?.setItem) return null
  try {
    storage.setItem(SHORTCUT_OVERRIDES_STORAGE_KEY, JSON.stringify(normalized))
  } catch {
    return null
  }
  try { target?.dispatchEvent(new CustomEvent(SHORTCUT_OVERRIDES_CHANGED_EVENT)) } catch {}
  return normalized
}

export function resolveShellCommands(overrides = readShortcutOverrides()) {
  const actions = normalizedOverrides(overrides).actions
  return SHELL_COMMAND_DEFINITIONS.map((definition) => {
    const override = actions[definition.id]
    const bindings = override?.disabled
      ? []
      : (override?.bindings || definition.bindings)
    return {
      ...definition,
      bindings: bindings.map(normalizeShortcutBinding).filter(Boolean),
      shortcutDisabled: override?.disabled === true,
    }
  })
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
    if (command.shortcutDisabled) continue
    if (command.bindings?.some(binding => shortcutMatches(event, binding))) return command
  }
  return null
}

export function frameShortcutBindings(commands, { reserveUnavailable = false } = {}) {
  return (Array.isArray(commands) ? commands : []).flatMap(command => (
    command.captureInMiniApps
      && !command.shortcutDisabled
      && (reserveUnavailable || command.reserveWhenUnavailable === true || command.enabled !== false)
      ? command.bindings.map(binding => ({ actionId: command.id, binding }))
      : []
  ))
}

export function shouldReserveShellShortcut(handled, standalone, command = null) {
  return handled === true || standalone === true || command?.reserveWhenUnavailable === true
}

export function shortcutLockCodes(commands) {
  return [...new Set(frameShortcutBindings(commands).map(({ binding }) => (
    binding.code || null
  )).filter(Boolean))]
}

export function shortcutReferenceGroups(commands) {
  const groups = new Map()
  for (const command of Array.isArray(commands) ? commands : []) {
    if (!Array.isArray(command?.shortcutLabels) || command.shortcutLabels.length === 0) continue
    const category = command.category || 'Other'
    if (!groups.has(category)) groups.set(category, [])
    groups.get(category).push(command)
  }
  return [...groups].map(([category, items]) => ({ category, items }))
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
