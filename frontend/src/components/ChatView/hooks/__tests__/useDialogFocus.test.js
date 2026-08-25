import { test } from 'node:test'
import assert from 'node:assert/strict'
import { dialogFocusableElements } from '../../../../hooks/useDialogFocus.js'

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
