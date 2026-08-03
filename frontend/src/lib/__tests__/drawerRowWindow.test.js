import test from 'node:test'
import assert from 'node:assert/strict'
import {
  DRAWER_INITIAL_WINDOW_ROWS,
  DRAWER_ROW_HEIGHT,
  DRAWER_ROW_OVERSCAN,
  clampDrawerRowWindow,
  drawerRowSpacerHeights,
  drawerRowWindow,
  drawerRowWindowContaining,
  drawerRowWindowForIndex,
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
  assert.ok(window.start >= 400 - DRAWER_ROW_OVERSCAN)
  assert.ok(
    window.end <= 400 + Math.ceil(860 / DRAWER_ROW_HEIGHT) + 2 * DRAWER_ROW_OVERSCAN,
  )
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

test('small scroll steps reuse one mounted drawer window', () => {
  const atBucketStart = drawerRowWindow({
    total: 795,
    scrollTop: 400 * DRAWER_ROW_HEIGHT,
    viewportHeight: 860,
    sectionTop: 0,
  })

  for (let row = 401; row < 400 + DRAWER_ROW_OVERSCAN; row += 1) {
    assert.deepEqual(
      drawerRowWindow({
        total: 795,
        scrollTop: row * DRAWER_ROW_HEIGHT,
        viewportHeight: 860,
        sectionTop: 0,
      }),
      atBucketStart,
      'native momentum must not swap React rows at every 40px boundary',
    )
  }

  assert.notDeepEqual(
    drawerRowWindow({
      total: 795,
      scrollTop: (400 + DRAWER_ROW_OVERSCAN) * DRAWER_ROW_HEIGHT,
      viewportHeight: 860,
      sectionTop: 0,
    }),
    atBucketStart,
    'the window still advances before the viewport can reach its overscan edge',
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

test('desktop active-chat reveal moves the window only for an unmounted Recent row', () => {
  const current = { start: 120, end: 168 }

  assert.equal(
    drawerRowWindowForIndex(current, 795, -1),
    current,
    'a pinned chat is absent from Recents and must not schedule a state update',
  )
  assert.equal(
    drawerRowWindowForIndex(current, 795, 140),
    current,
    'an already-mounted row must preserve object identity',
  )

  const moved = drawerRowWindowForIndex(current, 795, 620)
  assert.notEqual(moved, current)
  assert.ok(moved.start <= 620 && moved.end > 620)
  assert.equal(moved.end - moved.start, DRAWER_INITIAL_WINDOW_ROWS)
})
