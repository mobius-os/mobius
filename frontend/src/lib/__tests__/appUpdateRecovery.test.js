import test from 'node:test'
import assert from 'node:assert/strict'

import { appUpdateStaleMessage } from '../appUpdateRecovery.js'

test('explains how to recover a stale update without alarming about live state', () => {
  assert.equal(
    appUpdateStaleMessage({ appName: 'Reflection' }),
    'The pending update for Reflection changed upstream. Review the latest update and start again. The previous version is still running.',
  )
})
