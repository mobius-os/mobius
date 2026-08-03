import test from 'node:test'
import assert from 'node:assert/strict'
import { cidOf } from '../messageIdentity.js'

test('cidOf returns the row cid, else null (no read-time derivation)', () => {
  assert.equal(cidOf({ cid: 'abc', ts: 5 }), 'abc')
  assert.equal(cidOf({ cid: 'legacy-5', ts: 5 }), 'legacy-5')
  assert.equal(cidOf({ ts: 5 }), null)
  assert.equal(cidOf({}), null)
  assert.equal(cidOf(null), null)
})
