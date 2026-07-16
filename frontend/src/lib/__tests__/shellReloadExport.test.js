/**
 * Unit tests for the shellReload export from hooks/useNavigation.js.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/shellReloadExport.test.js
 *
 * The module-level IIFE in useNavigation.js consumes and removes the
 * 'shell-reload' sessionStorage key exactly once at import time. App.jsx
 * must import and use the exported `shellReload` value rather than calling
 * sessionStorage.getItem('shell-reload') again (dead branch — the key was
 * already removed). This test locks in two properties:
 *
 *   1. The exported value is truthy when the key was present at import time.
 *   2. The key is removed from sessionStorage after the module loads, so a
 *      second consumer would read null (validating the dead-branch claim).
 *
 * We can't actually import useNavigation in a plain Node environment (it
 * uses browser APIs), so we test the IIFE logic directly as a pure function.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

// Replicate the IIFE logic as a pure function for unit testing.
// This mirrors the exact code in hooks/useNavigation.js.
function parseShellReload(storage) {
  try {
    const raw = storage.get('shell-reload')
    if (!raw) return null
    storage.delete('shell-reload')
    try { return JSON.parse(raw) } catch { return null }
  } catch {
    return null
  }
}

test('shellReload: returns null when key is absent', () => {
  const storage = new Map()
  assert.equal(parseShellReload(storage), null)
})

test('shellReload: returns parsed value when key is present', () => {
  const storage = new Map([['shell-reload', JSON.stringify({ activeView: 'canvas', activeAppId: 42 })]])
  const result = parseShellReload(storage)
  assert.deepEqual(result, { activeView: 'canvas', activeAppId: 42 })
})

test('shellReload: removes the key after parsing (second reader sees null)', () => {
  const storage = new Map([['shell-reload', JSON.stringify({ activeView: 'chat' })]])
  parseShellReload(storage)
  // A second call (simulating App.jsx re-reading sessionStorage) must get null.
  assert.equal(parseShellReload(storage), null,
    'second consumer must see null — the IIFE already consumed the key')
})

test('shellReload: returns null on malformed JSON', () => {
  const storage = new Map([['shell-reload', 'not-valid-json{{']])
  assert.equal(parseShellReload(storage), null)
  // Key should still be removed even on parse failure.
  assert.equal(storage.has('shell-reload'), false, 'key must be removed even on parse error')
})

test('shellReload: returns null on empty string value', () => {
  const storage = new Map([['shell-reload', '']])
  // Empty raw → falsy check returns null before even trying JSON.parse.
  assert.equal(parseShellReload(storage), null)
})

test('shellReload: returns null when sandbox storage is unavailable', () => {
  const storage = {
    get() { throw new DOMException('Blocked by opaque sandbox', 'SecurityError') },
  }
  assert.equal(parseShellReload(storage), null)
})
