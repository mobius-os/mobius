import { test } from 'node:test'
import assert from 'node:assert/strict'
import { SEND_POST_TIMEOUT_MS } from '../useStreamConnection.js'

test('send timeout does not restore a draft before bounded backend waits finish', () => {
  assert.ok(SEND_POST_TIMEOUT_MS > 30_000,
    'a client abort must not race the backend legacy 30s checkout window')
})
