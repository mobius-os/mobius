import test from 'node:test'
import assert from 'node:assert/strict'
import {
  DESKTOP_SIDEBAR_STORAGE_KEY,
  readDesktopSidebarOpen,
  writeDesktopSidebarOpen,
} from '../useDesktopSidebar.js'

test('desktop sidebar defaults open and restores an explicit preference', () => {
  assert.equal(readDesktopSidebarOpen({ getItem: () => null }), true)
  assert.equal(readDesktopSidebarOpen({ getItem: () => 'true' }), true)
  assert.equal(readDesktopSidebarOpen({ getItem: () => 'false' }), false)
})

test('desktop sidebar storage is versioned, minimal, and failure-safe', () => {
  const writes = []
  writeDesktopSidebarOpen({
    setItem: (key, value) => writes.push([key, value]),
  }, false)
  assert.deepEqual(writes, [[DESKTOP_SIDEBAR_STORAGE_KEY, 'false']])

  assert.doesNotThrow(() => writeDesktopSidebarOpen({
    setItem: () => { throw new Error('blocked') },
  }, true))
  assert.equal(readDesktopSidebarOpen({
    getItem: () => { throw new Error('blocked') },
  }), true)
})
