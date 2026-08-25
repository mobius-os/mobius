import assert from 'node:assert/strict'
import test from 'node:test'
import {
  SHELL_SHORTCUTS,
  findShellShortcut,
  frameShortcutBindings,
  resolveShellCommands,
  shortcutLabel,
  shouldReserveShellShortcut,
  shortcutLockCodes,
  shortcutMatches,
} from '../keyboardShortcuts.js'

test('search uses the conventional Cmd/Ctrl+K chord without stealing variants', () => {
  const shortcut = SHELL_SHORTCUTS.openSearch
  assert.equal(shortcutMatches({ metaKey: true, key: 'k' }, shortcut), true)
  assert.equal(shortcutMatches({ ctrlKey: true, key: 'K' }, shortcut), true)
  assert.equal(shortcutMatches({ ctrlKey: true, shiftKey: true, key: 'k' }, shortcut), false)
  assert.equal(shortcutMatches({ ctrlKey: true, altKey: true, key: 'k' }, shortcut), false)
  assert.equal(shortcutMatches({ key: 'k' }, shortcut), false)
  assert.equal(shortcutMatches({ metaKey: true, key: 'k', repeat: true }, shortcut), false)
})

test('the command catalog distinguishes new chat from a new Builder tab', () => {
  const commands = resolveShellCommands()
  assert.equal(findShellShortcut({ metaKey: true, key: 'n' }, commands)?.id, 'chat.new')
  assert.equal(findShellShortcut({ metaKey: true, key: 't' }, commands)?.id, 'tab.newChat')
  assert.equal(findShellShortcut({ metaKey: true, shiftKey: true, key: 't' }, commands)?.id, 'workspace.reopenClosed')
  assert.equal(findShellShortcut({ metaKey: true, altKey: true, key: 't' }, commands), null)
})

test('only advertised global commands cross the mini-app boundary', () => {
  const commands = resolveShellCommands()
  const frameBindings = frameShortcutBindings(commands)
  assert.ok(frameBindings.some(item => item.actionId === 'search.open'))
  assert.ok(frameBindings.some(item => item.actionId === 'history.forward'))
  assert.equal(frameBindings.some(item => item.actionId === 'workspace.undo'), false)
  assert.equal(frameBindings.some(item => item.binding.key === 'z'), false)
  assert.deepEqual(shortcutLockCodes(commands), [
    'KeyK', 'KeyN', 'KeyT', 'KeyW', 'Backslash', 'Comma', 'Period',
  ])
})

test('disabled chords keep native browser behavior but stay reserved in an installed app', () => {
  const commands = resolveShellCommands().map(command => ({
    ...command,
    enabled: command.id !== 'tab.close',
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
  assert.equal(shouldReserveShellShortcut(false, false), false)
  assert.equal(shouldReserveShellShortcut(true, false), true)
  assert.equal(shouldReserveShellShortcut(false, true), true)
})

test('shortcut labels adapt to the owner platform', () => {
  assert.equal(shortcutLabel(SHELL_SHORTCUTS.openSearch, 'MacIntel'), '⌘K')
  assert.equal(shortcutLabel(SHELL_SHORTCUTS.openSearch, 'Win32'), 'Ctrl+K')
  assert.equal(shortcutLabel(SHELL_SHORTCUTS.toggleBuilder, 'MacIntel'), '⇧↵')
  assert.equal(shortcutLabel(SHELL_SHORTCUTS.toggleBuilder, 'Linux x86_64'), 'Shift+Enter')
  assert.equal(shortcutLabel({ key: ',', mod: true }, 'MacIntel'), '⌘,')
})
