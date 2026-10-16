// Queued messages are edited in the chat box; the tray only points there,
// and the chat box's primary button saves the open one.
import { test, after } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
after(() => vite.close())
const { default: QueuedMessages } = await vite.ssrLoadModule('/src/components/ChatView/QueuedMessages.jsx')
const { PrimaryAction } = await vite.ssrLoadModule('/src/components/ChatView/ChatInputBar.jsx')
const { composerDraftPreview, composerHistoryKeyHint } = await vite.ssrLoadModule('/src/components/ChatView/ComposerHistoryStrip.jsx')

const items = [
  { cid: 'q1', content: 'Then check the dark theme' },
  { cid: 'q2', content: 'Also update the changelog' },
]

test('the queued message open in the chat box is tagged instead of offering the pencil', () => {
  const html = renderToStaticMarkup(createElement(QueuedMessages, { items, inComposerCid: 'q2' }))
  assert.match(html, /queued__row queued__row--in-composer[^>]*>[\s\S]*Also update the changelog[\s\S]*In the chat box/)
  assert.equal(html.match(/aria-label="Edit queued message"/g)?.length, 1,
    'only the other row keeps its pencil')
})

test('the tray never renders its own editor', () => {
  const html = renderToStaticMarkup(createElement(QueuedMessages, { items }))
  assert.doesNotMatch(html, /<textarea/)
  assert.equal(html.match(/aria-label="Edit queued message"/g)?.length, 2)
})

test('the row open in the chat box offers no Send now, which would skip the edit there', () => {
  const html = renderToStaticMarkup(createElement(QueuedMessages, {
    items, inComposerCid: 'q2', steerActive: true,
  }))
  assert.equal(html.match(/aria-label="Send this queued message now"/g)?.length, 1,
    'only the other row can be sent now')
  assert.match(html, /queued__row--in-composer(?:(?!queued__row ).)*In the chat box/s)
  assert.doesNotMatch(html, /queued__row--in-composer(?:(?!queued__row ).)*queued__steer/s)
})

function primaryButton(props) {
  return renderToStaticMarkup(createElement(PrimaryAction, {
    sending: false, listening: false, hasInput: true, hasUploading: false,
    showSteer: false, steerReady: true, submissionBlocked: false,
    questionBlocked: false, onSubmit() {}, onStop() {}, onSteer() {},
    onToggleVoice() {}, ...props,
  }))
}

test('an open queued message can be saved while a question waits or the draft uploads', () => {
  const save = /<button[^>]*aria-label="Save queued message"[^>]*>/
  for (const blocked of [
    { questionBlocked: true },
    { hasUploading: true },
    { submissionBlocked: true },
    { sending: true, hasInput: false, showSteer: true },
  ]) {
    const html = primaryButton({ savesQueued: true, ...blocked })
    assert.match(html, save, JSON.stringify(blocked))
    assert.doesNotMatch(html.match(save)[0], /disabled/, JSON.stringify(blocked))
  }
  assert.match(primaryButton({ savesQueued: true, listening: true }), /aria-label="Stop recording"/,
    'dictation keeps its own stop button')
  assert.match(primaryButton({ questionBlocked: true }), /aria-label="Stop"/,
    'a draft still waits for the question card')
  assert.match(primaryButton({ hasUploading: true }), /aria-label="Send"[^>]*disabled/,
    'a draft still waits for its uploads')
})

test('the strip counts the draft\'s files, which stay with the draft', () => {
  assert.equal(composerDraftPreview('Then deploy it', 2), 'Then deploy it · 2 files')
  assert.equal(composerDraftPreview('', 1), '1 file')
  assert.equal(composerDraftPreview('Then deploy it'), 'Then deploy it')
})

test('screen readers learn that ↑ browses history, and how to move and leave once browsing', () => {
  assert.match(composerHistoryKeyHint({ hasEntries: true }), /Up arrow opens/)
  assert.match(composerHistoryKeyHint({ hasEntries: true, browsing: true }), /Up and Down[\s\S]*Escape returns to your draft/)
  assert.equal(composerHistoryKeyHint({ hasEntries: false }), null, 'nothing to browse, nothing to announce')
  assert.equal(composerHistoryKeyHint({ hasEntries: true, touch: true }), null, 'touch input has no arrow keys')
})


test('a queued preview never cuts an emoji in half', async () => {
  const { queuedPreview } = await import('../queuedPreview.js')
  const text = `a${'😀'.repeat(40)}`
  const { preview, needsTruncation } = queuedPreview(text)

  assert.equal(needsTruncation, true)
  assert.ok(preview.endsWith('😀…'), preview)
  assert.equal(preview.isWellFormed(), true)
})


test('a queued preview keeps short and multi-line text readable', async () => {
  const { queuedPreview } = await import('../queuedPreview.js')

  assert.deepEqual(queuedPreview('hi'), { preview: 'hi', needsTruncation: false })
  assert.deepEqual(
    queuedPreview('first\nsecond'),
    { preview: 'first …', needsTruncation: true },
  )
  assert.equal(queuedPreview('x'.repeat(81)).preview, `${'x'.repeat(80)}…`)
})
