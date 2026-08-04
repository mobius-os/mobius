import assert from 'node:assert/strict'
import test from 'node:test'
import {
  SHORTCUT_CATALOG,
  SHELL_SHORTCUTS,
  shortcutLabel,
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

test('shortcut labels adapt to the owner platform', () => {
  assert.equal(shortcutLabel(SHELL_SHORTCUTS.openSearch, 'MacIntel'), '⌘K')
  assert.equal(shortcutLabel(SHELL_SHORTCUTS.openSearch, 'Win32'), 'Ctrl+K')
  assert.equal(shortcutLabel(SHELL_SHORTCUTS.toggleBuilder, 'MacIntel'), '⇧↵')
  assert.equal(shortcutLabel(SHELL_SHORTCUTS.toggleBuilder, 'Linux x86_64'), 'Shift+Enter')
})

test('the code-owned catalog has stable unique action ids', () => {
  const ids = SHORTCUT_CATALOG.map(shortcut => shortcut.id)
  assert.deepEqual(ids, [
    'search.open',
    'workspace.undo',
    'workspace.toggle-builder',
  ])
  assert.equal(new Set(ids).size, ids.length)
})
