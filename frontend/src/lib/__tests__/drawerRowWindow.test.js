import test from 'node:test'
import assert from 'node:assert/strict'
import {
  DRAWER_INITIAL_WINDOW_ROWS,
  DRAWER_ROW_HEIGHT,
  clampDrawerRowWindow,
  drawerRowSpacerHeights,
  drawerRowWindow,
  drawerRowWindowContaining,
  initialDrawerRowWindow,
} from '../../components/Drawer/drawerRowWindow.js'

test('drawer keeps one bounded DOM window at the beginning of a long history', () => {
  assert.deepEqual(initialDrawerRowWindow(0), { start: 0, end: 0 })
  assert.deepEqual(initialDrawerRowWindow(12), { start: 0, end: 12 })
  assert.deepEqual(initialDrawerRowWindow(795), {
    start: 0,
    end: DRAWER_INITIAL_WINDOW_ROWS,
  })
})

test('scrolling to the middle slides the window instead of accumulating rows', () => {
  const window = drawerRowWindow({
    total: 795,
    scrollTop: 400 * DRAWER_ROW_HEIGHT,
    viewportHeight: 860,
    sectionTop: 0,
  })
  assert.ok(window.start > 380)
  assert.ok(window.end < 430)
  assert.ok(window.end - window.start < DRAWER_INITIAL_WINDOW_ROWS,
    'the mounted row count stays viewport-sized after an arbitrarily long scroll')

  const spacers = drawerRowSpacerHeights(window, 795)
  assert.equal(spacers.before, window.start * DRAWER_ROW_HEIGHT)
  assert.equal(spacers.after, (795 - window.end) * DRAWER_ROW_HEIGHT)
  assert.equal(
    spacers.before
      + (window.end - window.start) * DRAWER_ROW_HEIGHT
      + spacers.after,
    795 * DRAWER_ROW_HEIGHT,
    'windowing preserves the exact scroll extent',
  )
})

test('drawer windows clamp after deletion without growing the working set', () => {
  assert.deepEqual(
    clampDrawerRowWindow({ start: 760, end: 795 }, 80),
    { start: 45, end: 80 },
  )
})

test('desktop active-chat reveal mounts a far row in one bounded window', () => {
  const window = drawerRowWindowContaining(795, 620)
  assert.ok(window.start <= 620 && window.end > 620)
  assert.equal(window.end - window.start, DRAWER_INITIAL_WINDOW_ROWS)
})
