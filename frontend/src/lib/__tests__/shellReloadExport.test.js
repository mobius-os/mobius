/** Unit tests for the shared, one-shot shell-reload state reader. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { consumeShellReload, writeShellReload } from '../shellReloadState.js'

const APP_SOURCE = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8')
const NAV_SOURCE = readFileSync(
  new URL('../../hooks/useNavigation.js', import.meta.url),
  'utf8',
)

function fakeStorage(entries = []) {
  const values = new Map(entries)
  return {
    getItem(key) { return values.get(key) ?? null },
    removeItem(key) { values.delete(key) },
    has(key) { return values.has(key) },
  }
}

test('shellReload: a full session store cannot block the reload handoff', () => {
  const storage = {
    setItem() { throw new DOMException('Storage quota exceeded', 'QuotaExceededError') },
  }
  assert.equal(writeShellReload(storage, { activeView: 'canvas', activeAppId: 42 }), false)
})

test('shellReload: writes the one-shot snapshot when storage is available', () => {
  let written = null
  const storage = {
    setItem(key, value) { written = [key, value] },
  }
  const snapshot = { activeView: 'chat', activeChatId: 'chat-1' }
  assert.equal(writeShellReload(storage, snapshot), true)
  assert.deepEqual(written, ['shell-reload', JSON.stringify(snapshot)])
})

test('shellReload: returns null when key is absent', () => {
  assert.equal(consumeShellReload(fakeStorage()), null)
})

test('shellReload: returns parsed value when key is present', () => {
  const storage = fakeStorage([['shell-reload', JSON.stringify({ activeView: 'canvas', activeAppId: 42 })]])
  const result = consumeShellReload(storage)
  assert.deepEqual(result, { activeView: 'canvas', activeAppId: 42 })
})

test('shellReload: removes the key after parsing (second reader sees null)', () => {
  const storage = fakeStorage([['shell-reload', JSON.stringify({ activeView: 'chat' })]])
  consumeShellReload(storage)
  assert.equal(consumeShellReload(storage), null,
    'second consumer must see null — the IIFE already consumed the key')
})

test('shellReload: returns null on malformed JSON', () => {
  const storage = fakeStorage([['shell-reload', 'not-valid-json{{']])
  assert.equal(consumeShellReload(storage), null)
  assert.equal(storage.has('shell-reload'), false, 'key must be removed even on parse error')
})

test('shellReload: rejects legacy primitive markers', () => {
  const storage = fakeStorage([['shell-reload', '1']])
  assert.equal(consumeShellReload(storage), null)
  assert.equal(storage.has('shell-reload'), false)
})

test('shellReload: returns null on empty string value', () => {
  assert.equal(consumeShellReload(fakeStorage([['shell-reload', '']])), null)
})

test('shellReload: returns null when sandbox storage is unavailable', () => {
  const storage = {
    getItem() { throw new DOMException('Blocked by opaque sandbox', 'SecurityError') },
  }
  assert.equal(consumeShellReload(storage), null)
})

test('navigation is the only startup consumer of reload state', () => {
  assert.doesNotMatch(APP_SOURCE, /from '\.\/lib\/shellReloadState\.js'/)
  assert.match(NAV_SOURCE, /from '\.\.\/lib\/shellReloadState\.js'/)
  assert.doesNotMatch(APP_SOURCE, /from '\.\/hooks\/useNavigation\.js'/)
  assert.doesNotMatch(APP_SOURCE, /if \(shellReload\)/)
})
