import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../hooks/__tests__/react-hook-shim.mjs'
import {
  persistComposerDraft,
  readComposerDraft,
  readComposerHandoff,
  stageComposerHandoff,
} from '../composerDraft.js'
import {
  SEND_ATTEMPT_QUEUED_MESSAGE,
  SEND_ATTEMPT_UNCONFIRMED_MESSAGE,
  saveFailedSendAttempt,
  settleFailedSendConfirmation,
} from '../sendAttemptRecovery.js'
import useComposerDraftState from '../hooks/useComposerDraftState.js'
import useComposerHistory from '../hooks/useComposerHistory.js'
import { stageAppChatDraft } from '../../../lib/appChatNavigation.js'

function storageStub() {
  const values = new Map()
  return {
    get length() { return values.size },
    key(index) { return [...values.keys()][index] ?? null },
    getItem(key) { return values.has(key) ? values.get(key) : null },
    setItem(key, value) { values.set(key, String(value)) },
    removeItem(key) { values.delete(key) },
  }
}

test('restoration consumes ordinary handoffs but keeps autosend intent', () => {
  const previousStorage = globalThis.sessionStorage
  const storage = storageStub()
  globalThis.sessionStorage = storage
  try {
    stageComposerHandoff('ordinary-hook', 'Restore this', { autoSend: false })
    const ordinary = renderHook(() => useComposerDraftState({
      chatId: 'ordinary-hook',
      hidden: false,
      inputRef: { current: null },
    }))
    assert.equal(ordinary.result.current.pendingComposerSubmit, null)
    assert.deepEqual(readComposerHandoff('ordinary-hook'), {
      draft: null,
      autoSendDraft: null,
    })
    ordinary.unmount()

    stageComposerHandoff('autosend-hook', 'Send this once', { autoSend: true })
    const hook = renderHook(() => useComposerDraftState({
      chatId: 'autosend-hook',
      hidden: false,
      inputRef: { current: null },
    }))

    assert.deepEqual(hook.result.current.pendingComposerSubmit, {
      token: 'stored-handoff:autosend-hook',
      text: 'Send this once',
      storedHandoff: true,
    })
    assert.deepEqual(readComposerHandoff('autosend-hook'), {
      draft: 'Send this once',
      autoSendDraft: 'Send this once',
    })
    hook.unmount()
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
})

test('late transcript durability settles the mounted ambiguous-send owner', () => {
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  try {
    saveFailedSendAttempt('late-durable', {
      cid: 'cid-late',
      draftIdentity: 'draft-late',
      text: 'already sent',
      attachments: [{
        id: 'file-1', name: 'note.txt', size: 12, mime_type: 'text/plain',
      }],
    })
    const hook = renderHook(() => useComposerDraftState({
      chatId: 'late-durable',
      hidden: false,
      inputRef: { current: null },
    }))

    assert.equal(hook.result.current.input, 'already sent')
    assert.equal(hook.result.current.pendingFiles.length, 1)
    assert.equal(hook.result.current.reconcileFailedAttempt([], []), 'missing')
    assert.equal(hook.result.current.input, 'already sent')

    assert.equal(hook.result.current.reconcileFailedAttempt([
      { role: 'user', cid: 'cid-late' },
    ], []), 'durable')
    assert.equal(hook.result.current.input, '')
    assert.deepEqual(hook.result.current.pendingFiles, [])
    assert.equal(hook.result.current.sendFailure, null)
    assert.equal(hook.result.current.failedSendAttemptRef.current, null)
    hook.unmount()
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
})

test('an unavailable confirmation reaches the mounted ambiguous-send owner', () => {
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  try {
    saveFailedSendAttempt('confirmation-unavailable', {
      cid: 'cid-unconfirmed',
      draftIdentity: 'draft-unconfirmed',
      text: 'keep this draft',
      attachments: [],
    })
    const hook = renderHook(() => useComposerDraftState({
      chatId: 'confirmation-unavailable',
      hidden: false,
      inputRef: { current: null },
    }))

    assert.equal(hook.result.current.reconcileFailedAttempt(
      [],
      [],
      { reportUnavailable: true },
    ), 'unconfirmed')
    assert.equal(hook.result.current.input, 'keep this draft')
    assert.equal(
      hook.result.current.sendFailure,
      SEND_ATTEMPT_UNCONFIRMED_MESSAGE,
    )
    hook.unmount()
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
})

test('a hard-reloaded retained intent keeps text and files under automatic retry ownership', () => {
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  try {
    saveFailedSendAttempt('retained-reload', {
      cid: 'cid-retained',
      draftIdentity: 'draft-retained',
      text: 'retry automatically',
      attachments: [{
        id: 'file-retained', name: 'note.txt', size: 12, mime_type: 'text/plain',
      }],
    })
    const hook = renderHook(() => useComposerDraftState({
      chatId: 'retained-reload',
      hidden: false,
      inputRef: { current: null },
    }))
    const attempt = hook.result.current.failedSendAttemptRef.current

    assert.equal(hook.result.current.reconcileFailedAttempt(
      [],
      [],
      { reportQueued: true, expectedAttempt: attempt },
    ), 'queued')
    assert.equal(hook.result.current.input, 'retry automatically')
    assert.equal(hook.result.current.pendingFiles.length, 1)
    assert.equal(hook.result.current.sendFailure, SEND_ATTEMPT_QUEUED_MESSAGE)

    hook.result.current.handleComposerRemoveFile('file-retained')
    hook.result.current.handleComposerInputChange('new owner draft')
    const newerAttempt = {
      cid: 'cid-new',
      draftIdentity: 'draft-new',
      text: 'new owner draft',
      attachments: [],
    }
    hook.result.current.rememberFailedAttempt(newerAttempt)
    hook.result.current.setSendFailure('New attempt owns this status.')
    assert.equal(hook.result.current.reconcileFailedAttempt(
      [],
      [],
      { reportQueued: true, expectedAttempt: attempt },
    ), 'superseded')
    assert.equal(hook.result.current.input, 'new owner draft')
    assert.equal(hook.result.current.sendFailure, 'New attempt owns this status.')
    assert.deepEqual(hook.result.current.failedSendAttemptRef.current, newerAttempt)
    hook.unmount()
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
})

test('an older confirmation cannot relabel a newer failed attempt', async () => {
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  try {
    const hook = renderHook(() => useComposerDraftState({
      chatId: 'stale-confirmation',
      hidden: false,
      inputRef: { current: null },
    }))
    const first = {
      cid: 'cid-first', draftIdentity: 'draft-first', text: 'first', attachments: [],
    }
    hook.result.current.rememberFailedAttempt(first)

    let finishConfirmation
    const confirmation = settleFailedSendConfirmation(
      () => new Promise(resolve => { finishConfirmation = resolve }),
      options => hook.result.current.reconcileFailedAttempt(
        [],
        [],
        { ...options, expectedAttempt: first },
      ),
    )

    hook.result.current.handleComposerInputChange('second')
    const second = {
      cid: 'cid-second', draftIdentity: 'draft-second', text: 'second', attachments: [],
    }
    hook.result.current.rememberFailedAttempt(second)
    hook.result.current.setSendFailure('Second attempt is still queued.')
    finishConfirmation(null)

    assert.equal(await confirmation, 'superseded')
    assert.equal(hook.result.current.input, 'second')
    assert.equal(hook.result.current.sendFailure, 'Second attempt is still queued.')
    assert.deepEqual(hook.result.current.failedSendAttemptRef.current, second)
    hook.unmount()
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
})

test('a newer mounted draft supersedes late ambiguous-send confirmation', () => {
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  try {
    saveFailedSendAttempt('newer-draft', {
      cid: 'cid-old',
      draftIdentity: 'draft-old',
      text: 'old draft',
      attachments: [],
    })
    const hook = renderHook(() => useComposerDraftState({
      chatId: 'newer-draft',
      hidden: false,
      inputRef: { current: null },
    }))

    hook.result.current.handleComposerInputChange('newer draft')
    assert.equal(hook.result.current.reconcileFailedAttempt([
      { role: 'user', cid: 'cid-old' },
    ], []), 'none')
    assert.equal(hook.result.current.input, 'newer draft')
    hook.unmount()
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
})

// The chat box as ChatView and ChatInputBar wire it: the draft's durable owner
// and history browsing share one box.
const BROWSED = [
  { key: 's1', kind: 'sent', text: 'Fix the colour picker on mobile' },
]

function mountChatBox(chatId) {
  const hook = renderHook(() => {
    const draft = useComposerDraftState({ chatId, hidden: false, inputRef: { current: null } })
    const history = useComposerHistory({
      chatId,
      value: draft.input,
      sentHistory: BROWSED,
      queuedMessages: [],
      showValue: draft.handleComposerInputChange,
      saveQueued: async () => 'saved',
      draftAside: { set: draft.setDraftAside, take: draft.takeDraftBack },
    })
    return { draft, history }
  })
  return {
    get draft() { return hook.result.current.draft },
    get history() { return hook.result.current.history },
    type(text) {
      hook.result.current.history.noteEdit()
      hook.result.current.draft.handleComposerInputChange(text)
    },
    unmount: () => hook.unmount(),
  }
}

function withTabStorage(run) {
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  try { run() } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
}

test('switching chats while browsing history keeps the draft and its files, not the browsed message', () => {
  withTabStorage(() => {
    const file = { id: 'file-1', name: 'plan.txt', size: 12, mime_type: 'text/plain', status: 'done' }
    persistComposerDraft('browse-switch', 'my unfinished thought', [file])
    const box = mountChatBox('browse-switch')
    assert.equal(box.draft.input, 'my unfinished thought')

    assert.equal(box.history.move('older'), true)
    assert.equal(box.draft.input, 'Fix the colour picker on mobile')
    box.type('Fix the colour picker on mobile and tablet')
    assert.equal(readComposerDraft('browse-switch').input, 'my unfinished thought',
      'the browsed message never becomes the stored draft')

    box.unmount()
    const back = mountChatBox('browse-switch')
    assert.equal(back.history.browsing, false)
    assert.equal(back.draft.input, 'my unfinished thought')
    assert.deepEqual(back.draft.pendingFiles.map(f => f.name), ['plan.txt'])
    back.unmount()
  })
})

test('a pane shown again while browsing keeps the browsed message and returns the draft as edited elsewhere', () => {
  withTabStorage(() => {
    persistComposerDraft('browse-visible', 'first idea')
    const box = mountChatBox('browse-visible')
    box.history.move('older')

    persistComposerDraft('browse-visible', 'first idea, edited in another pane')
    box.draft.restoreDurableDraft()
    assert.equal(box.draft.input, 'Fix the colour picker on mobile', 'the box keeps what is browsed')
    assert.equal(box.history.browsing, true)

    box.history.move('newer')
    assert.equal(box.history.browsing, false)
    assert.equal(box.draft.input, 'first idea, edited in another pane')
    box.unmount()
  })
})

test('resending an earlier message keeps the draft stored, then brings it back', () => {
  withTabStorage(() => {
    persistComposerDraft('browse-resend', 'still writing this')
    const box = mountChatBox('browse-resend')
    box.history.move('older')
    assert.equal(box.history.submit(), 'send')

    box.draft.setComposerInput('')
    assert.equal(box.history.browsing, false)
    assert.equal(box.draft.input, 'still writing this')
    assert.equal(readComposerDraft('browse-resend').input, 'still writing this')
    box.unmount()
  })
})

test('an app draft keeps saved files and later owner edits survive hide and show', async () => {
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  try {
    const chatId = 'app-draft-files'
    const files = [{ name: 'notes.txt', size: 12, mime_type: 'text/plain', status: 'done' }]
    persistComposerDraft(chatId, 'Owner text', files)
    assert.equal(await stageAppChatDraft(chatId, 'App draft'), 'Owner text\n\nApp draft')
    let hidden = false
    const hook = renderHook(() => useComposerDraftState({ chatId, hidden, inputRef: { current: null } }))
    assert.equal(hook.result.current.input, 'Owner text\n\nApp draft')
    assert.equal(hook.result.current.pendingFiles[0].name, 'notes.txt')
    assert.equal(hook.result.current.pendingComposerSubmit, null)
    hook.result.current.handleComposerInputChange('Newer owner text')
    hidden = true
    hook.rerender()
    hidden = false
    hook.rerender()
    assert.equal(readComposerDraft(chatId).input, 'Newer owner text',
      'showing the chat again must not restore the app draft')
    assert.equal(hook.result.current.pendingFiles[0].name, 'notes.txt')
    hook.unmount()
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
})

test('work returned to the composer follows the owner draft and keeps its files', () => {
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  try {
    persistComposerDraft('returned-work', 'Owner draft', [{
      name: 'mine.txt', size: 3, mime_type: 'text/plain', status: 'done',
    }])
    const hook = renderHook(() => useComposerDraftState({
      chatId: 'returned-work',
      hidden: false,
      inputRef: { current: null },
    }))
    hook.result.current.appendToComposer('Queued follow-up', [
      { name: 'mine.txt', size: 3, mime_type: 'text/plain' },
      { name: 'queued.png', size: 9, mime_type: 'image/png' },
    ])
    assert.equal(hook.result.current.input, 'Owner draft\nQueued follow-up')
    assert.deepEqual(hook.result.current.pendingFiles.map(file => [file.name, file.status]), [
      ['mine.txt', 'done'], ['queued.png', 'done'],
    ])
    assert.deepEqual(readComposerDraft('returned-work'), {
      input: 'Owner draft\nQueued follow-up',
      attachments: [
        { name: 'mine.txt', size: 3, mime_type: 'text/plain', status: 'done' },
        { name: 'queued.png', size: 9, mime_type: 'image/png', status: 'done' },
      ],
    })
    hook.unmount()
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
})

test('returned files get their own chip ids, so removing one keeps the rest', t => {
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  const deleted = []
  t.mock.method(globalThis, 'fetch', (url, options) => {
    if (options?.method === 'DELETE') deleted.push(decodeURIComponent(url.split('/').pop()))
    return Promise.resolve({ ok: true })
  })
  const record = name => ({ name, size: 3, mime_type: 'text/plain', status: 'done' })
  try {
    persistComposerDraft('returned-ids', '', [record('a.txt'), record('b.txt')])
    const hook = renderHook(() => useComposerDraftState({
      chatId: 'returned-ids',
      hidden: false,
      inputRef: { current: null },
    }))
    hook.result.current.appendToComposer('', [record('q.png')])
    const ids = hook.result.current.pendingFiles.map(file => file.id)
    assert.equal(ids.every(Boolean), true)
    assert.equal(new Set(ids).size, 3)

    hook.result.current.handleComposerRemoveFile(ids[0])
    assert.deepEqual(hook.result.current.pendingFiles.map(file => file.name), ['b.txt', 'q.png'])
    assert.deepEqual(deleted, ['a.txt'])

    // A saved draft restored later gets ids the same way.
    persistComposerDraft('returned-ids', '', [record('c.txt'), record('d.txt')])
    hook.result.current.restoreDurableDraft()
    const restored = hook.result.current.pendingFiles.map(file => file.id)
    assert.equal(new Set(restored.filter(Boolean)).size, 2)
    hook.unmount()
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
})

test('returning only files retires an older ambiguous send like returned text', () => {
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  try {
    saveFailedSendAttempt('returned-files', {
      cid: 'cid-older', draftIdentity: 'draft-older', text: 'older', attachments: [],
    })
    const hook = renderHook(() => useComposerDraftState({
      chatId: 'returned-files',
      hidden: false,
      inputRef: { current: null },
    }))
    const older = hook.result.current.failedSendAttemptRef.current
    assert.ok(older)
    hook.result.current.appendToComposer('', [
      { name: 'queued.png', size: 9, mime_type: 'image/png' },
    ])
    assert.equal(hook.result.current.failedSendAttemptRef.current, null)
    assert.equal(hook.result.current.sendFailure, null)
    // The older send's late confirmation no longer clears the returned file.
    hook.result.current.reconcileFailedAttempt(
      [{ role: 'user', cid: 'cid-older', content: 'older' }], [],
      { expectedAttempt: older },
    )
    assert.deepEqual(hook.result.current.pendingFiles.map(file => file.name), ['queued.png'])
    hook.unmount()
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
})

test('text returned while history shows another message joins the set-aside draft', () => {
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = storageStub()
  try {
    persistComposerDraft('returned-history', 'Owner draft', [])
    const hook = renderHook(() => useComposerDraftState({
      chatId: 'returned-history',
      hidden: false,
      inputRef: { current: null },
    }))
    hook.result.current.setDraftAside('Owner draft')
    hook.result.current.setComposerInput('An earlier message')
    hook.result.current.appendToComposer('Queued follow-up')
    assert.equal(hook.result.current.input, 'An earlier message')
    assert.equal(readComposerDraft('returned-history').input, 'Owner draft\nQueued follow-up')
    assert.equal(hook.result.current.takeDraftBack(), 'Owner draft\nQueued follow-up')
    hook.unmount()
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
})
