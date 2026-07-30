import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')

test('app chat handoffs prove the target before navigating and preserve feedback', () => {
  const handler = shell.match(
    /if \(request\.type === 'moebius:open-chat'\) \{([\s\S]*?)\n      \}\n      if \(request\.type === 'moebius:open-app'\)/,
  )?.[1] || ''

  const probeAt = handler.indexOf('await probeDeletion(')
  const navigateAt = handler.indexOf("navToRef.current('chat', { chatId: request.chatId })")
  assert.ok(probeAt >= 0 && navigateAt > probeAt,
    'a missing target must not flash into the workspace before its 404 is known')
  assert.match(handler, /targetState === 'deleted'[\s\S]*newChatRef\.current\?\.\(\{[\s\S]*draft: draftText \|\| undefined,[\s\S]*forceNew: true,/,
    'deleted source chats must preserve the app draft in a durable fresh chat')
  assert.match(handler, /targetState === 'deleted'[\s\S]*return/,
    'the deleted-target fallback must not continue into stale navigation')
})

test('AppCanvas exclusively owns frame source attribution for workspace requests', () => {
  assert.match(shell, /<AppCanvas[\s\S]*onHostRequest=\{handleAppHostRequest\}/)
  assert.doesNotMatch(shell, /querySelectorAll\(['"]iframe\.canvas['"]\)/)
  assert.doesNotMatch(shell, /window\.addEventListener\(['"]message['"], onMessage\)/)
})
