import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

test('the composer state boundary saves before scheduling React state', () => {
  const source = readFileSync(
    new URL('../hooks/useComposerDraftState.js', import.meta.url),
    'utf8',
  )
  const start = source.indexOf('const setComposerInput = useCallback((nextInput) =>')
  const end = source.indexOf('\n  }, [chatId])', start)
  const body = source.slice(start, end)

  const save = body.indexOf('persistComposerDraft(chatId, nextInput, draftAttachmentsRef.current)')
  const render = body.indexOf('setInputState(nextInput)')
  assert.ok(save >= 0, 'all composer changes must be persisted directly')
  assert.ok(render > save, 'draft persistence must happen before navigation can unmount React')
})

test('shell draft handoffs use the same owner instead of writing around its live mirror', () => {
  const shellSource = readFileSync(
    new URL('../../Shell/Shell.jsx', import.meta.url),
    'utf8',
  )
  const chatSource = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  const directDraftWrite = /sessionStorage\.(?:setItem|removeItem)\(`draft:/
  assert.doesNotMatch(shellSource, directDraftWrite,
    'shell handoffs and deletion must not bypass the draft owner')
  assert.doesNotMatch(chatSource, directDraftWrite,
    'chat cleanup must clear memory, session, and durable copies together')
  assert.match(shellSource, /stageComposerHandoff\(buildingChatId, report\)/)
  assert.match(shellSource, /stageComposerHandoff\(request\.chatId, draftText\)/)
  assert.match(shellSource,
    /stageComposerHandoff\(chatId, handoff\.text, \{[\s\S]*?attachments: handoff\.attachments,[\s\S]*?autoSend: handoff\.autoSend/,
    'new-chat handoffs must preserve the settled text, attachments, and autosend intent')
  assert.match(shellSource, /consumeComposerHandoff\(prev\.chatId, prev\.draft\)/,
    'an acknowledged direct handoff must retire its global fallback')
  assert.match(shellSource, /requestComposer\(buildingChatId, \{ draft: report \}\)/,
    'a crash report must update a retained destination composer too')
  assert.match(shellSource, /clearComposerDraft\(id\)/)
  assert.match(chatSource, /clearComposerDraft\(chatId\)/)
})
