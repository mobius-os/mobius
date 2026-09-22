import { test } from 'node:test'
import assert from 'node:assert/strict'
import { dialogFocusableElements, dialogSiblingElements } from '../../../../hooks/useDialogFocus.js'

function visibleElement(overrides = {}) {
  return {
    hidden: false,
    getClientRects: () => [{}],
    ...overrides,
  }
}

test('native disclosure summaries participate in dialog focus traversal', () => {
  const summary = visibleElement()
  const container = {
    querySelectorAll(selector) {
      return selector.split(',').includes('summary') ? [summary] : []
    },
  }

  assert.deepEqual(dialogFocusableElements(container), [summary])
})

test('dialog focus traversal ignores hidden or unrendered controls', () => {
  const visible = visibleElement()
  const container = {
    querySelectorAll: () => [
      visibleElement({ hidden: true }),
      visibleElement({ getClientRects: () => [] }),
      visible,
    ],
  }

  assert.deepEqual(dialogFocusableElements(container), [visible])
})

test('local dialog inerting stops at its owning surface boundary', () => {
  const outsideSettings = {}
  const settings = { parentElement: null }
  const content = { parentElement: settings }
  const section = { parentElement: content }
  const dialog = { parentElement: section }
  const settingsControl = {}
  const otherSettingsSection = {}
  Object.assign(section, { children: [dialog, settingsControl] })
  Object.assign(content, { children: [section, otherSettingsSection] })
  Object.assign(settings, { children: [content] })
  // The outside sibling is intentionally not part of the walk: a pane-local
  // review must not inert a different workspace pane.
  settings.parentElement = { children: [settings, outsideSettings] }

  assert.deepEqual(dialogSiblingElements(dialog, settings), [settingsControl, otherSettingsSection])
})
