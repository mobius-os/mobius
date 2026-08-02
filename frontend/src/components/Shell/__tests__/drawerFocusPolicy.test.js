import { test } from 'node:test'
import assert from 'node:assert/strict'

import { shouldRestoreDrawerFocus } from '../../Drawer/drawerFocusPolicy.js'

test('drawer close restores only while the drawer still owns focus', () => {
  const inside = { id: 'inside' }
  const outside = { id: 'outside' }
  const body = { id: 'body' }
  const drawer = { contains: element => element === inside }

  assert.equal(shouldRestoreDrawerFocus({ drawer, activeElement: inside, body }), true)
  assert.equal(shouldRestoreDrawerFocus({ drawer, activeElement: body, body }), true)
  assert.equal(shouldRestoreDrawerFocus({ drawer, activeElement: null, body }), true)
  assert.equal(shouldRestoreDrawerFocus({ drawer, activeElement: outside, body }), false)
})
