import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')

test('app chat handoffs prove the target before navigating and preserve feedback', () => {
  const handler = shell.match(
    /else if \(e\.data\?\.type === 'moebius:open-chat'\) \{([\s\S]*?)\n      \} else if \(e\.data\?\.type === 'moebius:open-app'\)/,
  )?.[1] || ''

  const probeAt = handler.indexOf('await probeDeletion(')
  const navigateAt = handler.indexOf("navTo('chat', { chatId: e.data.chatId })")
  assert.ok(probeAt >= 0 && navigateAt > probeAt,
    'a missing target must not flash into the workspace before its 404 is known')
  assert.match(handler, /targetState === 'deleted'[\s\S]*newChatRef\.current\?\.\(\{[\s\S]*draft: draftText \|\| undefined,[\s\S]*forceNew: true,/,
    'deleted source chats must preserve the app draft in a durable fresh chat')
  assert.match(handler, /targetState === 'deleted'[\s\S]*return/,
    'the deleted-target fallback must not continue into stale navigation')
})
