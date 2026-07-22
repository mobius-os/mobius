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

test('sent image attachments match the composer card height and corners', () => {
  const composer = ruleBody('.chat__attach-card--image')
  const sentButton = ruleBody('.chat__attach-thumb-button')
  const sent = ruleBody('.chat__attach-thumb')

  assert.match(composer, /height:\s*96px/)
  assert.match(sent, /width:\s*96px/)
  assert.match(sent, /height:\s*96px/)
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
