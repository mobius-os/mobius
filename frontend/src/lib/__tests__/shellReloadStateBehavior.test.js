import test from 'node:test'
import assert from 'node:assert/strict'
import { writeShellReload } from '../shellReloadState.js'

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
