import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  shellReloadNavigationTransitionIsActive,
  supportsShellReloadNavigationTransition,
} from '../shellReloadNavigationTransition.js'

const supportedWindow = { onpageswap: null }
const supportedDocument = { startViewTransition() {} }

test('shell reload navigation transitions require cross-document browser support', () => {
  assert.equal(
    supportsShellReloadNavigationTransition(supportedWindow, supportedDocument),
    true,
  )
  assert.equal(supportsShellReloadNavigationTransition({}, supportedDocument), false)
  assert.equal(supportsShellReloadNavigationTransition(supportedWindow, {}), false)
})

test('the splash bypasses its fade only while a supported transition is active', () => {
  const activeRoot = { hasAttribute: () => true }
  const inactiveRoot = { hasAttribute: () => false }
  assert.equal(
    shellReloadNavigationTransitionIsActive(
      activeRoot,
      supportedWindow,
      supportedDocument,
    ),
    true,
  )
  assert.equal(
    shellReloadNavigationTransitionIsActive(activeRoot, {}, supportedDocument),
    false,
  )
  assert.equal(
    shellReloadNavigationTransitionIsActive(
      inactiveRoot,
      supportedWindow,
      supportedDocument,
    ),
    false,
  )
})
