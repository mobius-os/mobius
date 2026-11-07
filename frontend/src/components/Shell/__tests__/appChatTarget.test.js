import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')

test('app chat handoffs prove the target before navigating and preserve feedback', () => {
  const handler = shell.match(
    /if \(request\.type === 'moebius:open-chat'\) \{([\s\S]*?)\n      \}\n      if \(request\.type === 'moebius:open-app'\)/,
  )?.[1] || ''

  const strictAt = handler.indexOf('if (request.requireExisting) return openExistingChat(request, open)')
  const probeAt = handler.indexOf('await probeDeletion(')
  const navigateAt = handler.indexOf('return openChatWithAppDraft(request.chatId, request.draft, { open, openNew: openNewChat })')
  assert.ok(strictAt >= 0 && probeAt > strictAt && navigateAt > probeAt,
    'a missing target must not flash into the workspace before its 404 is known')
  assert.match(handler, /const openNewChat = async \(\) => \{[\s\S]*newChatRef\.current\?\.\(\{[\s\S]*draft: request\.draft \|\| undefined,[\s\S]*forceNew: true,/,
    'deleted source chats must preserve the app draft in a durable fresh chat')
  assert.match(handler, /if \(targetState === 'deleted'\) return openNewChat\(\)/,
    'the deleted-target fallback must not continue into stale navigation')
})

test('AppCanvas exclusively owns frame source attribution for workspace requests', () => {
  assert.match(shell, /<AppCanvas[\s\S]*onHostRequest=\{handleAppHostRequest\}/)
  assert.doesNotMatch(shell, /querySelectorAll\(['"]iframe\.canvas['"]\)/)
  assert.doesNotMatch(shell, /window\.addEventListener\(['"]message['"], onMessage\)/)
})

test('a crash report joins the building chat draft instead of replacing it', () => {
  const handler = shell.match(/const handleAppError = useCallback\(([\s\S]*?)\n  \}, \[/)?.[1] || ''
  assert.match(handler, /openChatWithAppDraft\(buildingChatId, report, \{[\s\S]*openNew: reportInNewChat,/,
    'the report is added after the owner draft, or starts a new chat when it cannot be')
  assert.doesNotMatch(handler, /stageComposerHandoff|requestComposer\(buildingChatId, \{ draft: report \}\)/,
    'the report alone must never replace the building chat composer')
})
