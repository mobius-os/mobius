import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const streamSource = readFileSync(
  new URL('../useStreamConnection.js', import.meta.url),
  'utf8',
)

test('send timeout does not restore a draft before bounded backend waits finish', () => {
  const match = streamSource.match(
    /const SEND_POST_TIMEOUT_MS\s*=\s*(\d+)/,
  )
  assert.ok(match, 'send POST timeout constant should remain explicit')
  assert.ok(Number(match[1]) > 30_000,
    'a client abort must not race the backend legacy 30s checkout window')
  assert.match(streamSource, /ambiguous\s*\n\/\/ outcome/,
    'keep the late-accept + restored-draft failure mode documented')
})
