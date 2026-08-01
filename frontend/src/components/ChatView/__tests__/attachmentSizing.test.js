import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
const msgContent = readFileSync(new URL('../MsgContent.jsx', import.meta.url), 'utf8')

function ruleBody(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`))
  assert.ok(match, `${selector} rule must exist`)
  return match[1]
}

test('roomy composer images match sent attachment size and corners', () => {
  const composer = ruleBody('.chat__attach-card--image')
  const sentButton = ruleBody('.chat__attach-thumb-button')
  const sent = ruleBody('.chat__attach-thumb')

  // Pending and sent squares share one roomy-size token instead of repeating a
  // literal. A pending card may compact below that ceiling when the keyboard
  // leaves very little room; its fallback must still be the sent-card token so
  // the normal layout and a token change remain in step.
  assert.match(ruleBody('.chat'), /--attach-card:\s*96px/)
  assert.match(composer, /height:\s*var\(--composer-attach-card,\s*var\(--attach-card/)
  assert.match(sent, /width:\s*var\(--attach-card/)
  assert.match(sent, /height:\s*var\(--attach-card/)
  assert.match(composer, /border-radius:\s*14px/)
  assert.match(sentButton, /border-radius:\s*14px/)
  assert.match(sent, /border-radius:\s*14px/)
})

test('sent attachments render above message text in both message paths', () => {
  const attachmentNeedle = "msg.role === 'user' && <Attachments"
  const firstAttachments = msgContent.indexOf(attachmentNeedle)
  const blockContent = msgContent.indexOf('{nodes.map(', firstAttachments)
  const secondAttachments = msgContent.indexOf(attachmentNeedle, firstAttachments + 1)
  const plainText = msgContent.indexOf('{text ? (', secondAttachments)

  assert.ok(firstAttachments >= 0 && firstAttachments < blockContent)
  assert.ok(secondAttachments >= 0 && secondAttachments < plainText)
})

test('a pending composer image opens the same full-screen viewer as a sent one', () => {
  const bar = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')

  // The thumbnail must be a real button (keyboard + a11y reachable), not a
  // click handler on the <img>, and must render the shared lightbox.
  assert.match(bar, /className="chat__attach-card-thumb-button"/)
  assert.match(bar, /setLightboxIndex\(/)
  assert.match(bar, /<ImageLightbox/)
  // Gallery paging across every attached image, so multi-attach can be checked
  // without closing and reopening.
  assert.match(bar, /items=\{gallery\}/)
  ruleBody('.chat__attach-card-thumb-button')
})
