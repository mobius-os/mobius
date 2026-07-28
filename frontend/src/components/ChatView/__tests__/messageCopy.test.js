import test from 'node:test'
import assert from 'node:assert/strict'

import { copyPlainText } from '../messageCopy.js'

test('copyPlainText treats empty output as not copied', async () => {
  assert.equal(await copyPlainText(''), false)
})
