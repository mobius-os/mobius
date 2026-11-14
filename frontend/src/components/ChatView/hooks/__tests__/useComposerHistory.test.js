import test from 'node:test'
import assert from 'node:assert/strict'

import { renderHook } from './react-hook-shim.mjs'
import useComposerHistory, { QUEUED_SAVE_NOTES } from '../useComposerHistory.js'

const SENT = [
  { key: 's1', kind: 'sent', text: 'Fix the colour picker on mobile' },
  { key: 's2', kind: 'sent', text: 'Add a test for the list menu' },
]
const QUEUE = [
  { cid: 'q1', content: 'Then check the dark theme' },
  { cid: 'q2', content: 'Also update the changelog' },
]

/** A chat box whose value is the hook's only display, like ChatInputBar. */
function mountComposer({ value = '', saveOutcome = 'saved', queue = QUEUE } = {}) {
  const saves = []
  let resolveSave = null
  const props = {
    chatId: 'chat-1',
    value,
    sentHistory: SENT,
    queuedMessages: queue,
    showValue: next => { props.value = next; hook.rerender() },
    saveQueued: (cid, text) => {
      saves.push({ cid, text })
      return new Promise(resolve => { resolveSave = resolve })
    },
  }
  const hook = renderHook(() => useComposerHistory(props))
  return {
    props,
    saves,
    get current() { return hook.result.current },
    type(next) { hook.result.current.noteEdit(); props.value = next; hook.rerender() },
    /** Mimics ChatView's doSend clearing the box after it accepts a send. */
    sendClearsBox() { props.value = ''; hook.rerender() },
    async settleSave(outcome = saveOutcome) { resolveSave(outcome); await new Promise(r => setTimeout(r, 0)) },
    rerender: () => hook.rerender(),
  }
}

test('Enter on a queued message saves it in place and brings the draft back', async () => {
  const box = mountComposer({ value: 'Then deploy it' })
  box.current.open('q1')
  assert.equal(box.props.value, 'Then check the dark theme')
  assert.equal(box.current.queuedKey, 'q1')
  box.type('Then check the dark theme and the README')

  assert.equal(box.current.submit(), 'handled', 'a queued edit is saved, never sent')
  assert.deepEqual(box.saves, [{ cid: 'q1', text: 'Then check the dark theme and the README' }])
  assert.equal(box.current.saving, true)

  await box.settleSave('saved')
  assert.equal(box.current.browsing, false)
  assert.equal(box.props.value, 'Then deploy it')
})

test('a queued message that started before the save keeps the text and becomes a new send', async () => {
  const box = mountComposer({ value: 'Then deploy it' })
  box.current.open('q2')
  box.type('Also update the changelog and the README')
  box.current.submit()
  await box.settleSave('gone')

  assert.equal(box.props.value, 'Also update the changelog and the README', 'nothing is lost')
  assert.equal(box.current.position.kind, 'started')
  assert.equal(box.current.submit(), 'send', 'Enter now sends it as a new message')
  box.sendClearsBox()
  assert.equal(box.props.value, 'Then deploy it', 'the draft returns after that send')
})

test('a save that cannot apply yet keeps the edit and says why', async () => {
  const box = mountComposer({ value: '' })
  box.current.open('q1')
  box.type('Then check the dark theme twice')
  box.current.submit()
  await box.settleSave('waiting')
  assert.equal(box.props.value, 'Then check the dark theme twice')
  assert.equal(box.current.note, QUEUED_SAVE_NOTES.waiting)
  box.type('Then check the dark theme twice!')
  assert.equal(box.current.note, null, 'editing again clears the note')
})

test('saving unchanged or empty queued text never calls the queue', () => {
  const box = mountComposer({ value: 'draft' })
  box.current.open('q1')
  box.type('   ')
  assert.equal(box.current.submit(), 'handled')
  assert.equal(box.current.note, QUEUED_SAVE_NOTES.empty)
  box.type('Then check the dark theme')
  box.current.submit()
  assert.deepEqual(box.saves, [])
  assert.equal(box.props.value, 'draft')
})

test('resending an earlier message brings the draft back once the send clears the box', () => {
  const box = mountComposer({ value: 'Also update the README' })
  box.current.move('older')
  box.current.move('older')
  box.current.move('older')
  assert.equal(box.props.value, 'Add a test for the list menu')
  box.type('Add a test for the list menu on small phones')

  assert.equal(box.current.submit(), 'send')
  box.sendClearsBox()
  assert.equal(box.current.browsing, false)
  assert.equal(box.props.value, 'Also update the README')
})

test('a chat switch never pulls the old chat’s draft into the new chat’s empty box', () => {
  const box = mountComposer({ value: 'my draft for the first chat' })
  box.current.move('older')
  box.current.move('older')
  box.current.move('older')
  assert.equal(box.props.value, 'Add a test for the list menu')
  assert.equal(box.current.submit(), 'send', 'arms the draft return in chat-1')
  // The send was refused (say, a file was still uploading), so the box was
  // never cleared — and the owner switches to a chat whose box is empty.
  box.props.chatId = 'chat-2'
  box.props.value = ''
  box.rerender()
  assert.equal(box.props.value, '', 'chat-2 keeps its own empty box')
  assert.equal(box.current.browsing, false)
})

test('a send that never happens leaves the recalled text and does not return later', () => {
  const box = mountComposer({ value: 'draft' })
  box.current.move('older')
  box.current.move('older')
  box.current.move('older')
  box.current.submit()
  // e.g. model selection was required, so the box was not cleared.
  box.type('')
  assert.equal(box.props.value, '', 'clearing the box by hand is an edit, not a send')
  assert.equal(box.current.browsing, true)
})

test('cancelling the queued message being edited gives the box back to the draft', () => {
  const box = mountComposer({ value: 'Then deploy it' })
  box.current.open('q1')
  assert.equal(box.current.setAside('q2'), false, 'another row does not affect the box')
  box.current.setAside('q1')
  assert.equal(box.props.value, 'Then deploy it')
  assert.equal(box.current.browsing, false)
})

test('a cancel that does not go through keeps the unsaved edit with its queued message', () => {
  const box = mountComposer({ value: 'Then deploy it' })
  box.current.open('q1')
  box.type('Then check the dark theme, edited')
  box.current.setAside('q1')
  assert.equal(box.props.value, 'Then deploy it', 'the draft comes back at once')

  // The queue still holds q1 (the cancel failed), so reopening finds the edit.
  box.current.open('q1')
  assert.equal(box.props.value, 'Then check the dark theme, edited')
})

test('Esc returns to the draft from anywhere in history', () => {
  const box = mountComposer({ value: 'my draft' })
  box.current.move('older')
  box.type('Also update the changelog, edited')
  assert.equal(box.current.escape(), true)
  assert.equal(box.props.value, 'my draft')
  assert.equal(box.current.escape(), false, 'outside history Esc stays native')
})
