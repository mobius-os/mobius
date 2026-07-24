import test from 'node:test'
import assert from 'node:assert/strict'
import {
  clampDesktopSidebarWidth,
  DESKTOP_SIDEBAR_DEFAULT_WIDTH,
  DESKTOP_SIDEBAR_MAX_WIDTH,
  DESKTOP_SIDEBAR_MIN_WIDTH,
  DESKTOP_SIDEBAR_STORAGE_KEY,
  DESKTOP_SIDEBAR_WIDTH_STORAGE_KEY,
  readDesktopSidebarOpen,
  readDesktopSidebarWidth,
  writeDesktopSidebarOpen,
  writeDesktopSidebarWidth,
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

test('desktop sidebar width is clamped, persisted, and failure-safe', () => {
  assert.equal(clampDesktopSidebarWidth('412.4'), 412)
  assert.equal(clampDesktopSidebarWidth(100), DESKTOP_SIDEBAR_MIN_WIDTH)
  assert.equal(clampDesktopSidebarWidth(1000), DESKTOP_SIDEBAR_MAX_WIDTH)
  assert.equal(clampDesktopSidebarWidth('bad'), DESKTOP_SIDEBAR_DEFAULT_WIDTH)
  assert.equal(readDesktopSidebarWidth({ getItem: () => null }), DESKTOP_SIDEBAR_DEFAULT_WIDTH)
  assert.equal(readDesktopSidebarWidth({ getItem: () => '448' }), 448)

  const writes = []
  writeDesktopSidebarWidth({
    setItem: (key, value) => writes.push([key, value]),
  }, 448)
  assert.deepEqual(writes, [[DESKTOP_SIDEBAR_WIDTH_STORAGE_KEY, '448']])

  assert.doesNotThrow(() => writeDesktopSidebarWidth({
    setItem: () => { throw new Error('blocked') },
  }, 400))
  assert.equal(readDesktopSidebarWidth({
    getItem: () => { throw new Error('blocked') },
  }), DESKTOP_SIDEBAR_DEFAULT_WIDTH)
})
