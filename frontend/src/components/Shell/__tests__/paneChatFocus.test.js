import test from 'node:test'
import assert from 'node:assert/strict'

import {
  shouldFocusComposerAfterPanePointer,
  supportsDesktopPaneComposerFocus,
} from '../paneChatFocus.js'

test('desktop pane composer focus requires a fine hovering pointer', () => {
  assert.equal(supportsDesktopPaneComposerFocus(() => ({ matches: true })), true)
  assert.equal(supportsDesktopPaneComposerFocus(() => ({ matches: false })), false)
  assert.equal(supportsDesktopPaneComposerFocus(undefined), false)
})

test('a primary mouse selection on non-interactive pane content can focus the composer', () => {
  const blank = { closest: () => null }
  assert.equal(shouldFocusComposerAfterPanePointer({
    wasFocused: false, pointerType: 'mouse', button: 0, target: blank,
  }), true)
  assert.equal(shouldFocusComposerAfterPanePointer({
    wasFocused: true, pointerType: 'mouse', button: 0, target: blank,
  }), false)
  assert.equal(shouldFocusComposerAfterPanePointer({
    wasFocused: false, pointerType: 'touch', button: 0, target: blank,
  }), false)
})

test('pane controls retain their native focus instead of handing it to the composer', () => {
  const control = { closest: () => ({ tagName: 'BUTTON' }) }
  assert.equal(shouldFocusComposerAfterPanePointer({
    wasFocused: false, pointerType: 'mouse', button: 0, target: control,
  }), false)
})

test('custom controls with an ARIA widget role keep the press, like native controls', () => {
  // Matches like Element.closest for an element carrying only this role.
  const withRole = role => ({
    closest: selector => selector.split(',').includes(`[role="${role}"]`) ? {} : null,
  })
  for (const role of ['radio', 'checkbox', 'switch', 'tab', 'option', 'button', 'link']) {
    assert.equal(shouldFocusComposerAfterPanePointer({
      wasFocused: false, pointerType: 'mouse', button: 0, target: withRole(role),
    }), false, role)
  }
  assert.equal(shouldFocusComposerAfterPanePointer({
    wasFocused: false, pointerType: 'mouse', button: 0, target: withRole('note'),
  }), true, 'non-widget roles are content')
})
