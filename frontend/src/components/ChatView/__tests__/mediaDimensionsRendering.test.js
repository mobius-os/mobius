/* A message's media_dimensions map describes its saved text. The chat often
 * shows newer text with that map (live stream, stream promotion, joined steer
 * replay), so only an explicit null from the server means "unreadable"; a path
 * the map does not mention keeps the default frame, and known sizes stay. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { promoteAssistantStream } from '../streamPromotion.js'
import { assistantReplyGroups, presentAssistantReply } from '../assistantReplies.js'

const shellLocation = { origin: 'http://localhost', href: 'http://localhost/shell/' }
globalThis.window = { location: shellLocation, innerHeight: 800 }
globalThis.location = shellLocation

// Server rendering has no DOM for DOMPurify. The image hrefs here are plain
// paths, so an identity sanitizer exercises the same URL checks.
const domPurifyStub = {
  name: 'dompurify-ssr-stub',
  enforce: 'pre',
  resolveId: id => (id === 'dompurify' ? '\0dompurify-stub' : null),
  load: id => (id === '\0dompurify-stub'
    ? 'export default { sanitize: value => String(value) }'
    : null),
}

const vite = await createServer({
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui', 'dompurify'] },
  plugins: [domPurifyStub],
})
const { default: MsgContent } = await vite.ssrLoadModule(
  '/src/components/ChatView/MsgContent.jsx',
)
const { default: AssistantReply } = await vite.ssrLoadModule(
  '/src/components/ChatView/AssistantReply.jsx',
)
const { imageDimensionsForHref, imageUnreadableForHref } = await vite.ssrLoadModule(
  '/src/components/ChatView/markdown/imageDims.js',
)

after(() => vite.close())

const chatId = 'chat-media-dims'
const A = `/api/chats/${chatId}/media/a.png`
const B = `/api/chats/${chatId}/media/b.png`
const BROKEN = `/api/chats/${chatId}/media/broken.png`
const sizeA = { width: 640, height: 480 }
const oldText = `First ![a](${A})`
const newText = `${oldText}\n\nThen ![b](${B})`

const assistant = (content, extras = {}) => ({
  role: 'assistant', id: 'run', content, blocks: [{ type: 'text', content }], ...extras,
})

function frames(html) {
  return {
    errors: (html.match(/md-image-error/g) || []).length,
    frames: (html.match(/class="md-image-frame"/g) || []).length,
    sizedA: html.includes('--md-image-ratio:640 / 480'),
    sizedB: html.includes('--md-image-ratio:300 / 600'),
  }
}

function renderMessage(msg) {
  return renderToStaticMarkup(createElement(MsgContent, {
    msg, chatId, isLastMsg: true, isStreaming: false,
  }))
}

test('only an explicit null marks an image unreadable', () => {
  const map = { [A]: sizeA, [BROKEN]: null }
  assert.deepEqual(imageDimensionsForHref(`${A}?preview=true`, map), sizeA)
  assert.equal(imageUnreadableForHref(BROKEN, map), true)
  assert.equal(imageUnreadableForHref(B, map), false, 'missing means unknown')
  assert.equal(imageUnreadableForHref(A, map), false)
  assert.equal(imageUnreadableForHref(B, undefined), false)

  const html = renderMessage(assistant(
    `![a](${A}) ![b](${B}) ![broken](${BROKEN})`,
    { media_dimensions: map },
  ))
  assert.deepEqual(frames(html), { errors: 1, frames: 2, sizedA: true, sizedB: false })
})

test('(a) stream promotion keeps known sizes and frames newer images', () => {
  const partial = assistant(oldText, { media_dimensions: { [A]: sizeA } })
  const [promoted] = promoteAssistantStream([partial], {
    items: [{ type: 'text', content: newText }],
    assistantMessageId: 'run',
  })
  assert.equal(promoted.content, newText)
  assert.deepEqual(frames(renderMessage(promoted)), { errors: 0, frames: 2, sizedA: true, sizedB: false })
})

test('(b) a live reply over a sized partial frames newly streamed images', () => {
  const partial = assistant(oldText, { media_dimensions: { [A]: sizeA } })
  const html = renderToStaticMarkup(createElement(AssistantReply, {
    replyGroup: assistantReplyGroups([partial]).get(0),
    activeMirrorMsg: partial,
    activitySourceBlocks: partial.blocks,
    useDbActivePayload: false,
    hasLivePayload: true,
    streamItems: [{ type: 'text', content: newText }],
    chatId,
    isStreaming: true,
  }))
  assert.deepEqual(frames(html), { errors: 0, frames: 2, sizedA: true, sizedB: false })
})

test('(c) a joined steer replay sizes images the later row knows', () => {
  const first = assistant(oldText, { media_dimensions: { [A]: sizeA } })
  const carrier = { role: 'user', hidden: true, steered: true, source_work_id: 'run', kind: 'peer_message' }
  const second = assistant(newText, {
    id: 'run:assistant:1',
    media_dimensions: { [A]: sizeA, [B]: { width: 300, height: 600 } },
  })
  const group = assistantReplyGroups([first, carrier, second]).get(0)
  assert.equal(group.rows.length, 2)
  const html = renderToStaticMarkup(createElement(AssistantReply, {
    replyGroup: group,
    activeRowIndex: 1,
    activeMirrorMsg: second,
    activitySourceBlocks: second.blocks,
    useDbActivePayload: true,
    hasLivePayload: false,
    chatId,
    isStreaming: false,
  }))
  // The first row displays the joined text, so it uses the later row's size for b.
  assert.deepEqual(frames(html), { errors: 0, frames: 2, sizedA: true, sizedB: true })
})

const carrierFor = id => ({ role: 'user', hidden: true, steered: true, source_work_id: id, kind: 'peer_message' })

function joinedRows(messages) {
  const group = assistantReplyGroups(messages).get(0)
  assert.equal(group.rows.length, messages.filter(m => m.role === 'assistant').length)
  return group.rows
}

test('a joined steer replay keeps one media map across recomputes', () => {
  const sizeB = { width: 300, height: 600 }
  const first = assistant(oldText, { media_dimensions: { [A]: sizeA } })
  const second = assistant(newText, { id: 'run:assistant:1', media_dimensions: { [B]: sizeB } })
  const rows = joinedRows([first, carrierFor('run'), second])
  const once = presentAssistantReply(rows)[0].message.media_dimensions
  const twice = presentAssistantReply(rows)[0].message.media_dimensions
  assert.deepEqual(once, { [A]: sizeA, [B]: sizeB })
  assert.equal(once, twice, 'memoized blocks need a stable map')

  // A later row without its own map leaves the owner's map untouched.
  const bare = assistant(newText, { id: 'run:assistant:1' })
  const bareRows = joinedRows([first, carrierFor('run'), bare])
  assert.equal(presentAssistantReply(bareRows)[0].message.media_dimensions, first.media_dimensions)
})

test('a joined steer replay prefers the later row for a shared path', () => {
  const first = assistant(oldText, { media_dimensions: { [A]: null } })
  const second = assistant(newText, { id: 'run:assistant:1', media_dimensions: { [A]: sizeA } })
  const rows = joinedRows([first, carrierFor('run'), second])
  assert.deepEqual(presentAssistantReply(rows)[0].message.media_dimensions, { [A]: sizeA })
})

test('a chained steer join accumulates every row\'s sizes', () => {
  const C = `/api/chats/${chatId}/media/c.png`
  const sizeB = { width: 300, height: 600 }
  const sizeC = { width: 100, height: 100 }
  const lastText = `${newText}\n\nFinally ![c](${C})`
  const first = assistant(oldText, { media_dimensions: { [A]: sizeA } })
  const second = assistant(newText, { id: 'run:assistant:1', media_dimensions: { [B]: { width: 1, height: 1 } } })
  const third = assistant(lastText, { id: 'run:assistant:2', media_dimensions: { [B]: sizeB, [C]: sizeC } })
  const rows = joinedRows([first, carrierFor('run'), second, carrierFor('run'), third])
  const presented = presentAssistantReply(rows)
  assert.equal(presented[0].message.blocks[0].content, lastText)
  assert.deepEqual(presented[0].message.media_dimensions, { [A]: sizeA, [B]: sizeB, [C]: sizeC })
  assert.equal(presentAssistantReply(rows)[0].message.media_dimensions, presented[0].message.media_dimensions)
})
