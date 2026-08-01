import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

function ruleBody(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`))
  assert.ok(match, `${selector} rule must exist`)
  return match[1]
}

// The composer grew to a fixed 280px of text PLUS whatever the attach tray
// added on top (another 102px), with no relationship to the space left on
// screen. On a phone with the soft keyboard open that composer was taller
// than the entire visible chat: the conversation disappeared and the pill
// read as "growing forever". Its cap has to come from the visible room.
test('the composer text area caps against the visible room, not a constant', () => {
  const input = ruleBody('.chat__input')

  assert.match(input, /max-height:\s*clamp\(/)
  assert.match(input, /var\(--composer-room/)
  assert.match(input, /var\(--composer-reserve/)
  // 280px stays the ceiling on a roomy screen, and growth past the cap
  // scrolls inside the textarea rather than resizing the pill.
  assert.match(input, /280px/)
  assert.match(input, /overflow-y:\s*auto/)
})

test('an attached file comes out of the text share, not on top of it', () => {
  // Whatever the chip tray occupies is reserved from the same budget, so the
  // pill's total height is the same with and without an attachment.
  assert.match(ruleBody('.chat__pill'), /--composer-reserve:\s*8px/)
  assert.match(
    ruleBody('.chat__pill--with-attach'),
    /--composer-reserve:\s*calc\(var\(--attach-card\)/,
  )
})

test('ChatView publishes the visible room from the smaller of pane and viewport', () => {
  // `.chat` is a fixed layer on an unscrollable document: on a phone it keeps
  // its full height while the keyboard covers half of it, so only
  // visualViewport reports the shrink. In a split workspace the pane is the
  // smaller of the two. The cap needs whichever is smaller.
  assert.match(chatView, /--composer-room/)
  assert.match(chatView, /window\.visualViewport\?\.height/)
  assert.match(chatView, /Math\.min\(visible \|\| pane, pane \|\| visible\)/)
})
