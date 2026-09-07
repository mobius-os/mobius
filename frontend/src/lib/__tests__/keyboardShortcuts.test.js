import assert from 'node:assert/strict'
import test from 'node:test'
import {
  SHELL_SHORTCUTS,
  SHORTCUT_OVERRIDES_STORAGE_KEY,
  findShellShortcut,
  frameShortcutBindings,
  readShortcutOverrides,
  resolveShellCommands,
  shortcutLabel,
  shouldReserveShellShortcut,
  shortcutLockCodes,
  shortcutMatches,
  shortcutReferenceGroups,
  writeShortcutOverrides,
} from '../keyboardShortcuts.js'

class MemoryStorage {
  constructor(value = null) {
    this.values = new Map(value == null ? [] : [[SHORTCUT_OVERRIDES_STORAGE_KEY, value]])
  }
  getItem(key) { return this.values.get(key) ?? null }
  setItem(key, value) { this.values.set(key, String(value)) }
}

test('search uses the conventional Cmd/Ctrl+K chord without stealing variants', () => {
  const shortcut = SHELL_SHORTCUTS.openSearch
  assert.equal(shortcutMatches({ metaKey: true, key: 'k' }, shortcut), true)
  assert.equal(shortcutMatches({ ctrlKey: true, key: 'K' }, shortcut), true)
  assert.equal(shortcutMatches({ ctrlKey: true, shiftKey: true, key: 'k' }, shortcut), false)
  assert.equal(shortcutMatches({ ctrlKey: true, altKey: true, key: 'k' }, shortcut), false)
  assert.equal(shortcutMatches({ key: 'k' }, shortcut), false)
  assert.equal(shortcutMatches({ metaKey: true, key: 'k', repeat: true }, shortcut), false)
})

test('keyboard help uses Cmd/Ctrl+slash as a separate shell surface', () => {
  const shortcut = SHELL_SHORTCUTS.openShortcutHelp
  assert.equal(shortcutMatches({ metaKey: true, key: '/' }, shortcut), true)
  assert.equal(shortcutMatches({ ctrlKey: true, key: '/' }, shortcut), true)
  assert.equal(shortcutMatches({ metaKey: true, shiftKey: true, key: '/' }, shortcut), false)
})

test('the command catalog distinguishes new chat from a new Builder tab', () => {
  const commands = resolveShellCommands({ version: 1, actions: {} })
  assert.equal(findShellShortcut({ metaKey: true, key: 'n' }, commands)?.id, 'chat.new')
  assert.equal(findShellShortcut({ metaKey: true, key: 't' }, commands)?.id, 'tab.newChat')
  assert.equal(findShellShortcut({ metaKey: true, shiftKey: true, key: 't' }, commands)?.id, 'workspace.reopenClosed')
  assert.equal(findShellShortcut({ metaKey: true, altKey: true, key: 't' }, commands), null)
})

test('owner overrides change or disable bindings without rewriting the catalog', () => {
  const storage = new MemoryStorage()
  writeShortcutOverrides({
    actions: {
      'search.open': { bindings: [{ key: 'p', code: 'KeyP', mod: true }] },
      'pane.close': { disabled: true },
      'future.action': { bindings: [{ key: '7', mod: true }] },
      'unsafe.action': { bindings: [{ key: 'x' }] },
    },
  }, { storage, target: null })

  const stored = readShortcutOverrides(storage)
  assert.ok(stored.actions['future.action'], 'future ids survive version skew')
  assert.deepEqual(stored.actions['unsafe.action'].bindings, [])
  assert.ok(storage.getItem(SHORTCUT_OVERRIDES_STORAGE_KEY))
  const commands = resolveShellCommands(stored)
  assert.equal(findShellShortcut({ ctrlKey: true, key: 'p' }, commands)?.id, 'search.open')
  assert.equal(findShellShortcut({ ctrlKey: true, key: 'k' }, commands), null)
  assert.equal(commands.find(command => command.id === 'pane.close').shortcutDisabled, true)
  assert.equal(commands.some(command => command.id === 'future.action'), false)
})

test('a blocked preference write reports that nothing was saved', () => {
  const storage = { setItem() { throw new Error('blocked') } }
  assert.equal(writeShortcutOverrides({ actions: {} }, { storage, target: null }), null)
})

test('malformed stored overrides fail closed to the default catalog', () => {
  assert.deepEqual(readShortcutOverrides(new MemoryStorage('{broken')), {
    version: 1,
    actions: {},
  })
  assert.equal(
    findShellShortcut(
      { ctrlKey: true, key: 'k' },
      resolveShellCommands(readShortcutOverrides(new MemoryStorage('{broken'))),
    )?.id,
    'search.open',
  )
})

test('only advertised global commands cross the mini-app boundary', () => {
  const commands = resolveShellCommands({ version: 1, actions: {} })
  const frameBindings = frameShortcutBindings(commands)
  assert.ok(frameBindings.some(item => item.actionId === 'search.open'))
  assert.ok(frameBindings.some(item => item.actionId === 'history.forward'))
  assert.equal(frameBindings.some(item => item.actionId === 'workspace.undo'), false)
  assert.equal(frameBindings.some(item => item.binding.key === 'z'), false)
  assert.deepEqual(shortcutLockCodes(commands), [
    'KeyK', 'Slash', 'KeyN', 'KeyT', 'KeyW', 'Backslash', 'Comma', 'Period',
  ])
})

test('disabled chords keep native browser behavior except commands that must not leak to it', () => {
  const commands = resolveShellCommands({ version: 1, actions: {} }).map(command => ({
    ...command,
    enabled: command.id !== 'tab.close' && command.id !== 'history.back',
  }))

  assert.equal(
    frameShortcutBindings(commands).some(item => item.actionId === 'tab.close'),
    false,
  )
  assert.equal(
    frameShortcutBindings(commands, { reserveUnavailable: true })
      .some(item => item.actionId === 'tab.close'),
    true,
  )
  assert.equal(
    frameShortcutBindings(commands).some(item => item.actionId === 'history.back'),
    true,
    'Cmd/Ctrl+, must stay in Möbius when there is no Back destination',
  )
  assert.equal(shouldReserveShellShortcut(false, false), false)
  assert.equal(shouldReserveShellShortcut(true, false), true)
  assert.equal(shouldReserveShellShortcut(false, true), true)
  assert.equal(
    shouldReserveShellShortcut(false, false, commands.find(command => command.id === 'history.back')),
    true,
  )
})

test('an owner-disabled chord never crosses the mini-app boundary', () => {
  const commands = resolveShellCommands({
    version: 1,
    actions: { 'tab.close': { disabled: true } },
  }).map(command => ({ ...command, enabled: false }))

  assert.equal(
    frameShortcutBindings(commands, { reserveUnavailable: true })
      .some(item => item.actionId === 'tab.close'),
    false,
  )
})

test('shortcut reference groups the resolved catalog and omits unbound actions', () => {
  const commands = [
    { id: 'search.open', category: 'Workspace', shortcutLabels: ['⌘K'] },
    { id: 'shortcuts.open', category: 'Workspace', shortcutLabels: ['⌘/'] },
    { id: 'pane.close', category: 'Tabs and panes', shortcutLabels: [] },
  ]
  assert.deepEqual(shortcutReferenceGroups(commands), [{
    category: 'Workspace',
    items: commands.slice(0, 2),
  }])
})

test('shortcut labels adapt to the owner platform', () => {
  assert.equal(shortcutLabel(SHELL_SHORTCUTS.openSearch, 'MacIntel'), '⌘K')
  assert.equal(shortcutLabel(SHELL_SHORTCUTS.openSearch, 'Win32'), 'Ctrl+K')
  assert.equal(shortcutLabel(SHELL_SHORTCUTS.toggleBuilder, 'MacIntel'), '⇧↵')
  assert.equal(shortcutLabel(SHELL_SHORTCUTS.toggleBuilder, 'Linux x86_64'), 'Shift+Enter')
  assert.equal(shortcutLabel({ key: ',', mod: true }, 'MacIntel'), '⌘,')
})
