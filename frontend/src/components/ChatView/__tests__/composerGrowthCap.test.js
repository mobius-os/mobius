import { test } from 'node:test'
import assert from 'node:assert/strict'

import { composerRoom } from '../composerTextareaSizing.js'

// The composer grew to a fixed 280px of text PLUS whatever the attach tray
// added on top of it (another 102px), with no relationship to the space left
// on screen. On a phone with the soft keyboard open that composer was taller
// than the entire visible chat: the conversation disappeared behind it and the
// pill read as "growing forever, never scrolling".
//
// SCOPE: `composerRoom` is a pure function and these cases exercise its
// arithmetic only — the number ChatView publishes as `--composer-room`. They
// say nothing about the CSS clamp that consumes it, nor about whether the var
// reaches `.chat` at all. Both of those are browser behaviour and are covered
// in tests/composer-growth-cap.spec.mjs.

test('the soft keyboard shrinks the room even though the pane does not', () => {
  // The reported phone, keyboard up. `.chat` is a fixed layer on an
  // unscrollable document, so it still measures its full 754px while the
  // keyboard covers the bottom ~340px. Trusting the pane here is precisely
  // what let a 406px composer cover the whole visible conversation.
  assert.equal(composerRoom({ paneHeight: 754, viewportHeight: 412 }), 412)
})

test('a tiled pane bounds the room when the window is the larger of the two', () => {
  // Desktop split workspace, no keyboard: the window is tall, this chat's
  // pane is not, and the composer belongs to the pane.
  assert.equal(composerRoom({ paneHeight: 360, viewportHeight: 1080 }), 360)
})

test('either bound stands alone while the other is still unknown', () => {
  // A retained pane is display:none and reports clientHeight 0 until it is
  // shown; a non-browser runtime has no visualViewport. An unknown bound must
  // not win the min and starve the composer down to its 48px floor.
  assert.equal(composerRoom({ paneHeight: 0, viewportHeight: 412 }), 412)
  assert.equal(composerRoom({ paneHeight: 754, viewportHeight: 0 }), 754)
})

test('an unmeasurable pane and viewport report zero rather than a guess', () => {
  // Zero is the caller's signal to publish nothing at all, which leaves
  // `.chat__input` on the default baked into its clamp(). Returning a bogus
  // small number instead would cap the composer at its 48px floor.
  assert.equal(composerRoom(), 0)
  assert.equal(composerRoom({}), 0)
  assert.equal(composerRoom({ paneHeight: NaN, viewportHeight: -1 }), 0)
})

test('fractional viewport heights publish whole pixels', () => {
  // visualViewport.height is fractional on a zoomed or scaled phone, and a
  // subpixel value in a CSS var makes the cap jitter by a pixel per resize.
  assert.equal(composerRoom({ paneHeight: 754, viewportHeight: 411.5 }), 412)
  assert.equal(composerRoom({ paneHeight: 411.4, viewportHeight: 754 }), 411)
})
