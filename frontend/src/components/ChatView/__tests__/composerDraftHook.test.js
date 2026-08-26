import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../hooks/__tests__/react-hook-shim.mjs'
import {
  readComposerHandoff,
  stageComposerHandoff,
} from '../composerDraft.js'
import { saveFailedSendAttempt } from '../sendAttemptRecovery.js'
import useComposerDraftState from '../hooks/useComposerDraftState.js'

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
