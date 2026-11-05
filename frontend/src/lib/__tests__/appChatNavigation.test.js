/* App chat navigation only navigates: no fallback, draft replacement or send. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import 'fake-indexeddb/auto'
import { createStore, set as setIdbValue } from 'idb-keyval'
import { saveFailedSendAttempt } from '../../components/ChatView/sendAttemptRecovery.js'
import { openExistingChat, stageAppChatDraft } from '../appChatNavigation.js'
import {
  _clearComposerDraftMemoryForTests, persistComposerDraft, readComposerDraft, readComposerHandoff,
} from '../../components/ChatView/composerDraft.js'

function storageStub() {
  const values = new Map()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

async function withBrowser(fetch, run) {
  const previousFetch = globalThis.fetch
  const previousStorage = globalThis.sessionStorage
  globalThis.fetch = fetch
  globalThis.sessionStorage = storageStub()
  try { await run() } finally {
    globalThis.fetch = previousFetch
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
}

test('open-existing-chat probes the exact chat and stages only for one that exists', async () => {
  const files = [{ name: 'note.txt', size: 2, mime_type: 'text/plain', status: 'done' }]
  for (const [status, outcome] of [[404, 'target_missing'], [503, 'unavailable'], [403, 'unavailable'], [200, 'opened']]) {
    const chatId = `strict-${status}`
    await withBrowser(async url => {
      assert.match(String(url), new RegExp(`/chats/${chatId}\\?limit=1&compact=1$`))
      return new Response('{}', { status })
    }, async () => {
      persistComposerDraft(chatId, 'Owner text', files)
      const opened = []
      const result = await openExistingChat({ chatId, draft: 'App draft', autoSend: true }, draft => opened.push(draft))
      assert.deepEqual(result, { outcome })
      assert.deepEqual(opened, status === 200 ? ['Owner text\n\nApp draft'] : [])
      assert.deepEqual(readComposerDraft(chatId), {
        input: status === 200 ? 'Owner text\n\nApp draft' : 'Owner text', attachments: files,
      })
      assert.equal(readComposerHandoff(chatId).autoSendDraft, null)
    })
  }
})

test('a failed send awaiting resolution keeps the composer from app drafts', async () => {
  await withBrowser(async () => new Response('{}'), async () => {
    persistComposerDraft('ambiguous', 'Owner text', [])
    saveFailedSendAttempt('ambiguous', { cid: 'prior', draftIdentity: 'prior-draft', text: 'Owner text', attachments: [] })
    await assert.rejects(stageAppChatDraft('ambiguous', 'App draft'), /resolve its previous message/)
    const outcome = await openExistingChat({ chatId: 'ambiguous', draft: 'App draft' }, () => assert.fail('must not navigate'))
    assert.deepEqual(outcome, { outcome: 'unavailable' })
    assert.equal(readComposerDraft('ambiguous').input, 'Owner text')
    assert.equal(await stageAppChatDraft('ambiguous', ''), undefined, 'no draft stages nothing')
  })
})

test('a cold start keeps a newer session draft over an older durable one', async () => {
  const envelope = (updatedAt, input, attachments = []) => JSON.stringify({
    type: 'mobius-composer-draft', version: 2, updated_at: updatedAt, input, attachments,
  })
  const file = { name: 'newer.txt', size: 3, mime_type: 'text/plain' }
  await withBrowser(async () => new Response('{}'), async () => {
    await setIdbValue('cold', envelope(1, 'Older text'), createStore('mobius-owner-drafts', 'drafts-v1'))
    // The durable write of the newer draft failed; only session storage has it.
    sessionStorage.setItem('draft:cold', envelope(2, 'Newer text', [file]))
    _clearComposerDraftMemoryForTests()
    assert.equal(await stageAppChatDraft('cold', 'App draft'), 'Newer text\n\nApp draft')
    assert.deepEqual(readComposerDraft('cold'), {
      input: 'Newer text\n\nApp draft', attachments: [{ ...file, status: 'done' }],
    })
  })
})
