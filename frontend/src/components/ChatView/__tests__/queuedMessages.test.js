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

test('a held queue is sent from its tray while the empty composer keeps the mic', () => {
  const sendHeld = /<button[^>]*aria-label="Send queued messages"[^>]*>/
  const html = renderToStaticMarkup(createElement(QueuedMessages, {
    items: items.map(row => ({ ...row, held: true })), onSendHeld() {},
  }))
  assert.match(html, sendHeld)
  assert.match(html, /Waiting for you — sends when you send/)
  assert.doesNotMatch(renderToStaticMarkup(createElement(QueuedMessages, { items })), sendHeld,
    'an ordinary queue has no send-all control')
  assert.match(primaryButton({ hasInput: false }), /aria-label="Voice input"/,
    'dictation stays reachable')
})

test('held rows never claim to send after a running turn', () => {
  const html = renderToStaticMarkup(createElement(QueuedMessages, {
    items: items.map(row => ({ ...row, held: true })), turnActive: true,
  }))
  assert.match(html, /Waiting for you — sends when you send/)
  assert.doesNotMatch(html, /after the current turn/)
  assert.doesNotMatch(html, /aria-label="Send queued messages"/)
})

test('a shared-browser guest sees the held queue waiting for the owner, with no send', () => {
  const html = renderToStaticMarkup(createElement(QueuedMessages, {
    items: items.map(row => ({ ...row, held: true })), sharedGuest: true,
  }))
  assert.match(html, /Waiting for the owner to send/)
  assert.doesNotMatch(html, /aria-label="Send queued messages"/)
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


test('a queued message that is only a picture shows its file name, never a blank row', () => {
  const picture = { name: 'image.png', size: 30000, mime_type: 'image/png' }
  const sentForm = { cid: 'q1', content: '', attachments: [picture] }
  // Once the server has the message, its text is only the hidden file manifest.
  const storedForm = {
    cid: 'q2',
    content: '\n\n[Files in this session:\n- image.png → /data/chats/c/uploads/image.png (image/png, 30 KB)]',
    attachments: [picture],
  }
  for (const item of [sentForm, storedForm]) {
    const html = renderToStaticMarkup(createElement(QueuedMessages, { items: [item] }))
    assert.match(html, /queued__files queued__files--only[^>]*>[\s\S]*image\.png/, item.cid)
    assert.doesNotMatch(html, /queued__text/, `${item.cid} has no text to show`)
  }
})

test('a queued message with text and files keeps its text and names the files after it', () => {
  const html = renderToStaticMarkup(createElement(QueuedMessages, {
    items: [{
      cid: 'q1',
      content: 'Check this layout',
      attachments: [{ name: 'a.png' }, { name: 'b.png' }, { name: 'c.png' }],
    }],
  }))
  assert.match(html, /queued__text[^>]*>Check this layout[\s\S]*queued__files"[^>]*>[\s\S]*a\.png \+2 more/)
})

test('a rejected picture-only message still says it was kept, beside its file', () => {
  const html = renderToStaticMarkup(createElement(QueuedMessages, {
    items: [{ cid: 'q1', content: '', attachments: [{ name: 'image.png' }], recoveryAvailable: true }],
  }))
  assert.match(html, /Send rejected\. Your message is kept here\.[\s\S]*queued__files--only[\s\S]*image\.png/)
})

test('a queued message without files shows no file label', async () => {
  const { queuedFilesLabel } = await import('../queuedPreview.js')
  assert.equal(queuedFilesLabel(undefined), '')
  assert.equal(queuedFilesLabel([]), '')
  assert.equal(queuedFilesLabel([{ name: 'one.pdf' }]), 'one.pdf')
  assert.doesNotMatch(
    renderToStaticMarkup(createElement(QueuedMessages, { items })),
    /queued__files/,
  )
})

test('a message queued during a turn is not captioned as waiting for the owner', () => {
  const html = renderToStaticMarkup(createElement(QueuedMessages, {
    items: [{ ...items[0], held: true }, items[1]], turnActive: true,
  }))
  assert.match(html, /1 waiting for you; the rest send after this turn/)
})
