/* A formatted answer stays lossless on both sides of a visible steer. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { createServer } from 'vite'
import { renderWithModels } from './modelRegistryRender.js'

const location = { origin: 'http://localhost', href: 'http://localhost/shell/' }
globalThis.window = { location, innerWidth: 900 }
globalThis.location = location
const vite = await createServer({ appType: 'custom', logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui', 'dompurify'] },
  // SSR has no DOM; image fixtures use safe source URLs, not sanitizer behavior.
  plugins: [{ name: 'dompurify-ssr-stub', enforce: 'pre',
    resolveId: id => id === 'dompurify' ? '\0dompurify-stub' : null,
    load: id => id === '\0dompurify-stub'
      ? 'export default { sanitize: value => String(value) }' : null }] })
after(() => vite.close())
const { default: Message } = await vite.ssrLoadModule('/src/components/ChatView/MsgContent.jsx')
const { default: Reply } = await vite.ssrLoadModule('/src/components/ChatView/AssistantReply.jsx')
const { projectSteerContinuationMessage, projectSettledSteerContinuations, projectActiveSteerPrefix } = await vite.ssrLoadModule('/src/components/ChatView/steerContinuity.js')
const { assistantReplyGroups, presentAssistantReply } = await vite.ssrLoadModule('/src/components/ChatView/assistantReplies.js')
const { PeerTimelineContext } = await vite.ssrLoadModule('/src/components/ChatView/peerTimelineContext.js')
const { assistantClipboardText } = await vite.ssrLoadModule('/src/components/ChatView/markdownClipboard.js')
const { Marked } = await vite.ssrLoadModule('marked')
const { MemoBlock } = await vite.ssrLoadModule('/src/components/ChatView/markdown/blocks.jsx')
const { sliceMarkdownRange } = await vite.ssrLoadModule('/src/components/ChatView/markdown/steerMarkdownRange.js')
const assistant = (content, id = 'run') => ({ role: 'assistant', id, content, blocks: [{ type: 'text', content }] })
const prefix = 'Earlier explanation.\n\n**3. Don'
const full = 'Earlier explanation.\n\n**3. Don’t confuse uncertainty with failure—or success.**\n\nLater explanation.'
const steer = { role: 'user', steered: true, content: 'Also check the earlier PRs.' }
const saved = [assistant(prefix), steer, assistant(full, 'run:assistant:1')]
function render(Component, props, positions = new Map()) {
  return renderWithModels(createElement(PeerTimelineContext.Provider,
    { value: { tools: new Map(), positions } }, createElement(Component, props)))
}
function message(msg) { return render(Message, { msg, chatId: 'fixture', messageKey: msg.id }) }
function assertPair(html) {
  assert.equal(html.split('Earlier explanation.').length - 1, 1)
  assert.equal(html.split('Later explanation.').length - 1, 1)
  assert.match(html, /<strong>3\. Don<\/strong>/)
  assert.match(html, /<strong>’t confuse uncertainty with failure—or success\.<\/strong>/)
  assert.ok(html.indexOf('3. Don') < html.indexOf(steer.content))
  assert.ok(html.indexOf(steer.content) < html.indexOf('’t confuse'))
  assert.doesNotMatch(html, /\*\*3\.|success\.\*\*/)
}

test('reload renders the incident as two formatted ranges around the same owner row', () => {
  const original = JSON.stringify(saved)
  const shown = projectSettledSteerContinuations(saved)
  assertPair(shown.map(message).join(''))
  assert.equal(JSON.stringify(saved), original, 'presentation must never rewrite saved transcript')
})

for (const use of ['[ref][foo]', '![diagram][foo]']) {
  for (const whole of [false, true]) {
    test(`projected memo blocks update for late references, not unrelated prose (use=${use}, whole=${whole})`, () => {
      const prefix = `${use}${whole ? '\n\n' : ' '}**Pla`
      const replay = prefix + 'nned**'
      function range(source) {
        return projectSettledSteerContinuations([assistant(prefix), steer,
          assistant(source, 'run:assistant:1')])[0].blocks[0].markdown_range
      }
      const unresolved = range(replay)
      const resolved = range(replay + '\n\n[foo]: https://example.com/target "Title"')
      const changed = range(replay + '\n\n[foo]: https://example.com/new "New title"')
      const extended = range(replay + '\n\n[foo]: https://example.com/target "Title"\n\nLater prose.')
      const first = value => ({ token: value.tokens[0] })
      assert.equal(first(unresolved).token.raw, first(resolved).token.raw)
      assert.equal(MemoBlock.compare(first(unresolved), first(resolved)), false,
        'a late definition must replace literal text with its parsed link/image')
      assert.equal(MemoBlock.compare(first(resolved), first(changed)), false,
        'source target and title updates must reach mounted content')
      assert.equal(MemoBlock.compare(first(resolved), first(unresolved)), false,
        'removing a definition must not retain stale linked content')
      assert.equal(MemoBlock.compare(first(resolved), first(extended)), true,
        'unrelated streamed prose must not rerender the unchanged prefix')
      const sliceEnd = whole ? use.length : prefix.length - 1
      assert.equal(MemoBlock.compare(first(sliceMarkdownRange(unresolved, 0, sliceEnd)),
        first(sliceMarkdownRange(resolved, 0, sliceEnd))), false,
      'positioned activity slices retain reference-dependent render context')
    })
  }
}

test('active text_final and saved reload render the same content without a repeated prefix', () => {
  const projected = projectSteerContinuationMessage(saved[0], saved[2], { active: true })
  const groups = assistantReplyGroups(projectActiveSteerPrefix(projectSettledSteerContinuations(saved), { continuationIndex: 2, continuation: projected }))
  const before = render(Reply, { replyGroup: groups.get(0), activeRowIndex: -1,
    activeMirrorMsg: groups.get(0).rows[0].message, useDbActivePayload: true, chatId: 'fixture' })
  for (const useDbActivePayload of [false, true]) {
    const after = render(Reply, { replyGroup: groups.get(2), activeRowIndex: 0,
      activeMirrorMsg: saved[2], useDbActivePayload, hasLivePayload: !useDbActivePayload,
      streamItems: saved[2].blocks, sealedSteerAssistant: saved[0], isStreaming: true, chatId: 'fixture' })
    assertPair(before + message(steer) + after)
  }
})

for (const legacy of [false, true]) {
  test(`DB and settled Reply never trim an already-projected repeated-prefix suffix (legacy=${legacy})`, () => {
    const sealed = assistant('**ab ')
    const source = assistant('**ab **ab cd**ef**', 'run:assistant:1')
    if (legacy) { delete sealed.blocks; delete source.blocks }
    const shown = projectSettledSteerContinuations([sealed, steer, source])
    const group = assistantReplyGroups(shown).get(2)
    const expected = '<strong><strong>ab cd</strong>ef</strong>'
    for (const activeRowIndex of [-1, 0]) {
      const html = render(Reply, { replyGroup: group, activeRowIndex,
        activeMirrorMsg: group.rows[0].message, useDbActivePayload: true,
        sealedSteerAssistant: sealed, isStreaming: activeRowIndex === 0, chatId: 'fixture' })
      assert.ok(html.includes(expected), html)
      assert.doesNotMatch(html, /ef\*\*/)
    }
    const live = render(Reply, { replyGroup: group, activeRowIndex: 0,
      activeMirrorMsg: group.rows[0].message, useDbActivePayload: false, hasLivePayload: true,
      streamItems: [{ type: 'text', content: source.content }],
      sealedSteerAssistant: sealed, isStreaming: true, chatId: 'fixture' })
    assert.ok(live.includes(expected), 'fresh live payloads must replace inherited projection metadata')
  })
}

test('multiple visible steers retain each exact character interval', () => {
  const middle = '**3. Don’t confuse'
  const rows = [assistant('**3. Don'), steer, assistant(middle, 'run:assistant:1'),
    { ...steer, content: 'And the tests.' }, assistant('**3. Don’t confuse things.**', 'run:assistant:2')]
  const shown = projectSettledSteerContinuations(rows)
  const html = shown.map(message).join('')
  assert.equal(shown[2].blocks[0].content, '’t confuse')
  assert.equal(shown[4].blocks[0].content, ' things.**')
  assert.equal(html.split('3. Don').length - 1, 1)
  assert.equal(html.split('’t confuse').length - 1, 1)
  assert.match(html, /<strong> things\.<\/strong>/)
  assert.match(html, /<strong>3\. Don<\/strong>/)
  assert.match(html, /<strong>’t confuse<\/strong>/)
  assert.doesNotMatch(html, /\*\*/)
})

test('recorded activity slices a formatted continuation without losing its context', () => {
  const shown = projectSettledSteerContinuations(saved)
  const note = { id: 'peer-note', sender_chat_id: 'peer', sender_name: 'Colleague',
    body: 'New information', created_at: 2000,
    display_position: { assistant_message_id: 'run:assistant:1', block_index: 0, text_offset: full.indexOf('Later explanation.') } }
  const html = render(Message, { msg: shown[2], chatId: 'fixture', messageKey: 'run:assistant:1' },
    new Map([['run:assistant:1', [note]]]))
  assert.match(html, /<strong>’t confuse uncertainty with failure—or success\.<\/strong>/)
  assert.equal(html.split('Later explanation.').length - 1, 1)
  assert.ok(html.indexOf('’t confuse') < html.indexOf('Received from Colleague'))
  assert.ok(html.indexOf('Received from Colleague') < html.indexOf('Later explanation.'))
})

test('pre-existing activity offsets do not trim the sealed prefix a second time', () => {
  const rows = structuredClone(saved)
  rows[0].blocks[0].source_text_offset = 4
  const shown = projectSettledSteerContinuations(rows)
  assertPair(shown.map(message).join(''))
})

for (const legacy of [false, true]) test(`final live parse reaches every earlier steer (legacy=${legacy})`, () => {
  const rows = [assistant('**3. Don'), steer, assistant('**3. Don’t', 'run:assistant:1'),
    { ...steer, content: 'Second steer' }]
  if (legacy) rows.forEach(row => { delete row.blocks })
  const original = JSON.stringify(rows)
  const continuation = projectSteerContinuationMessage(rows[2], assistant('**3. Don’t continue**', 'run:assistant:2'), { active: true })
  const shown = projectActiveSteerPrefix(projectSettledSteerContinuations(rows), { continuationIndex: 4, continuation })
  const html = [...shown, continuation].map(message).join('')
  assert.match(html, /<strong>3\. Don<\/strong>/)
  assert.match(html, /<strong>’t<\/strong>/)
  assert.match(html, /<strong> continue<\/strong>/)
  assert.doesNotMatch(html, /\*\*/)
  assert.equal(JSON.stringify(rows), original)
})

for (const firstJoined of [false, true]) test(`hidden replay chain formats all fragments across activity (firstJoined=${firstJoined})`, () => {
  const source = ['**3. Don', '**3. Don’t', '**3. Don’t continue**'].map((text, i) => ({
    key: `row-${i}`, message: assistant(text, i ? `run:assistant:${i}` : 'run'),
    notes: i > 0 && (!firstJoined || i === 2) ? [{ id: `note-${i}` }] : [],
  }))
  const original = JSON.stringify(source)
  const html = presentAssistantReply(source).map(row => message(row.message)).join('')
  assert.doesNotMatch(html, /\*\*/)
  assert.match(html, firstJoined ? /<strong>3\. Don’t<\/strong>/ : /<strong>3\. Don<\/strong>/)
  assert.match(html, /<strong> continue<\/strong>/)
  assert.equal(JSON.stringify(source), original)
})

test('live completed formatting wins over a lagging saved active mirror', () => {
  const rows = [assistant('**3. Don'), steer, assistant('**3. Don’t', 'run:assistant:1'),
    { ...steer, content: 'Second steer' }, assistant('**3. Don’t continue', 'run:assistant:2')]
  const continuation = projectSteerContinuationMessage(rows[2], assistant('**3. Don’t continue**', 'run:assistant:2'), { active: true })
  const shown = projectActiveSteerPrefix(projectSettledSteerContinuations(rows), { continuationIndex: 4, continuation })
  const html = shown.slice(0, 4).map(message).join('')
  assert.match(html, /<strong>3\. Don<\/strong>/)
  assert.match(html, /<strong>’t<\/strong>/)
  assert.doesNotMatch(html, /\*\*/)
})

// Exercise the rendered component's actual copy handler and source map. The
// selection covers the whole displayed block, where raw-source copy wins.
function copyWholeFragment(msg, visibleText) {
  let onCopy
  function Capture() {
    const surface = Message.type({ msg, chatId: 'fixture', messageKey: msg.id })
    onCopy = surface.type(surface.props).props.onCopy
    return surface
  }
  const html = render(Capture, {})
  const text = { nodeType: 3, nodeValue: visibleText, textContent: visibleText }
  const strong = { nodeType: 1, tagName: 'STRONG', childNodes: [text] }
  // Rendered image wrappers contain an authorized URL, unlike their source.
  const image = { nodeType: 1, tagName: 'BUTTON', childNodes: [{ nodeType: 1,
    tagName: 'IMG', childNodes: [], getAttribute: name => name === 'src'
      ? '/api/media/authorized-preview?test-authorization=do-not-copy' : 'diagram' }] }
  const paragraph = { nodeType: 1, tagName: 'P', childNodes:
    msg.content.includes('![diagram]') ? [image, strong] : [strong] }
  const fragment = { nodeType: 11, childNodes: [paragraph] }
  const block = { dataset: { assistantMarkdownBlock: '0' }, contains: () => true }
  const range = {
    startContainer: block, endContainer: block, commonAncestorContainer: block,
    startOffset: 0, endOffset: 1, collapsed: false,
    intersectsNode: node => node === block,
    selectNodeContents() {}, setStart() {}, setEnd() {},
    toString: () => visibleText, cloneContents: () => fragment,
    cloneRange() { return { ...this } },
  }
  const ownerDocument = {
    createRange: () => ({ ...range }),
    getSelection: () => ({ rangeCount: 1, isCollapsed: false, getRangeAt: () => range }),
  }
  block.ownerDocument = ownerDocument
  const payload = new Map()
  const clipboardData = { setData: (type, value) => payload.set(type, value), getData: type => payload.get(type) || '' }
  let prevented = false
  onCopy({ currentTarget: { ownerDocument, querySelectorAll: () => [block] }, clipboardData,
    preventDefault: () => { prevented = true } })
  assert.equal(prevented, true)
  return { html, clipboardData }
}

for (const legacy of [false, true]) test(`whole formatted fragments copy balanced Markdown for normal paste (legacy=${legacy})`, () => {
  const rows = [assistant('**3. Don'), steer, assistant('**3. Don’t continue**', 'run:assistant:1')]
  if (legacy) rows.forEach(row => { delete row.blocks })
  const shown = projectSettledSteerContinuations(rows)
  for (const [msg, visibleText, markdown] of [
    [shown[0], '3. Don', '**3\\. Don**'],
    [shown[2], '’t continue', '**’t continue**'],
  ]) {
    const { html, clipboardData } = copyWholeFragment(msg, visibleText)
    assert.ok(html.includes(`<strong>${visibleText}</strong>`))
    assert.equal(assistantClipboardText(clipboardData), markdown)
    assert.equal(assistantClipboardText(clipboardData, true), visibleText)
  }
})

for (const legacy of [false, true]) test(`range copy preserves source images alongside split emphasis (legacy=${legacy})`, () => {
  const image = '![diagram](https://example.com/diagram.png)'
  const prefix = `${image} **Plan`
  const rows = [assistant(prefix), steer, assistant(`${prefix}ned** ${image}`, 'run:assistant:1')]
  if (legacy) rows.forEach(row => { delete row.blocks })
  const shown = projectSettledSteerContinuations(rows)
  for (const [msg, visibleText, expected] of [
    [shown[0], 'Plan', `${image} **Plan**`],
    [shown[2], 'ned', `**ned** ${image}`],
  ]) {
    const { html, clipboardData } = copyWholeFragment(msg, visibleText)
    assert.ok(html.includes('aria-label="Open diagram preview"'), 'the selected block includes the rendered source image')
    assert.equal(assistantClipboardText(clipboardData), expected)
    assert.ok(!assistantClipboardText(clipboardData).includes('test-authorization'))
    assert.equal(assistantClipboardText(clipboardData, true), visibleText)
  }
})

test('ordinary whole-block copy retains the original Markdown source', () => {
  const { clipboardData } = copyWholeFragment(assistant('__ordinary__'), 'ordinary')
  assert.equal(assistantClipboardText(clipboardData), '__ordinary__')
})

for (const legacy of [false, true]) {
  for (const [prefix, replay] of [
    ['**Number &#42;** then **Plan', '**Number &#42;** then **Planned**'],
    ['_a', '_ab **cd**ef_'],
  ]) {
    test(`whole-fragment copying retains entity and mixed-style meaning (legacy=${legacy}, prefix=${prefix})`, () => {
      const rows = [assistant(prefix), steer, assistant(replay, 'run:assistant:1')]
      if (legacy) rows.forEach(row => { delete row.blocks })
      const shown = projectSettledSteerContinuations(rows)
      const md = new Marked()
      for (const msg of [shown[0], shown[2]]) {
        const range = msg.blocks?.[0].markdown_range ?? msg.markdown_range
        const { clipboardData } = copyWholeFragment(msg, 'Selected text')
        assert.equal(md.parse(assistantClipboardText(clipboardData)), md.parser(range.tokens))
      }
    })
  }
}

for (const legacy of [false, true]) {
  for (const use of ['[ref][foo]', '[foo][]', '[foo]', '![diagram][foo]', '![foo][]', '![foo]']) {
    test(`reference atoms retain source targets on both sides of copy (legacy=${legacy}, use=${use})`, () => {
      const definition = '[foo]: /api/media/source.png "Source title"'
      const prefix = `${use} **bo`
      const rows = [assistant(prefix), steer,
        assistant(`${use} **bold end** ${use}\n\n${definition}\n[unrelated]: https://example.com/hidden`, 'run:assistant:1')]
      if (legacy) rows.forEach(row => { delete row.blocks })
      const shown = projectSettledSteerContinuations(rows)
      for (const [msg, visibleText, expected] of [
        [shown[0], 'bo', `${use} **bo**\n\n${definition}`],
        [shown[2], 'ld end', `**ld end** ${use}\n\n${definition}`],
      ]) {
        const { clipboardData } = copyWholeFragment(msg, visibleText)
        const copied = assistantClipboardText(clipboardData)
        assert.equal(copied, expected)
        assert.ok(new Marked().parse(copied).includes('/api/media/source.png'))
        assert.ok(!copied.includes('example.com/hidden'))
        assert.ok(!copied.includes('test-authorization'))
      }
    })
  }
}

for (const legacy of [false, true]) {
  for (const [source, marker, tag] of [['**ab **cde**fg**', '**', 'strong'], ['~~ab ~~cde~~fg~~', '~~', 'del']]) {
    test(`nested ${tag} copy keeps prefix and suffix formatting (legacy=${legacy})`, () => {
      const rows = [assistant(source.slice(0, 5)), steer, assistant(source, 'run:assistant:1')]
      if (legacy) rows.forEach(row => { delete row.blocks })
      const shown = projectSettledSteerContinuations(rows)
      for (const [msg, text] of [[shown[0], 'ab'], [shown[2], 'cdefg']]) {
        const { html, clipboardData } = copyWholeFragment(msg, text)
        assert.ok(html.includes(`<${tag}>`), 'display retains its original formatting')
        assert.equal(assistantClipboardText(clipboardData), `${marker}${text}${marker}`)
        assert.equal(assistantClipboardText(clipboardData, true), text)
      }
      assert.equal(rows[2].content, source, 'copy never changes authoritative prose')
    })
  }
}

for (const earlierAssistant of [false, true]) test(`ID-less active formatting targets the sealed predecessor, never an earlier row (earlierAssistant=${earlierAssistant})`, () => {
  const earlier = earlierAssistant ? assistant('**Old') : { role: 'user', content: 'Initial prompt' }
  const rows = [earlier, { role: 'user', content: 'Next prompt' }, assistant('**Plan'), steer]
  const next = assistant('**Planned** maintenance')
  rows.forEach(row => { delete row.id })
  delete next.id
  const original = JSON.stringify(rows)
  const continuation = projectSteerContinuationMessage(rows[2], next, { active: true })
  const settled = projectSettledSteerContinuations(rows)
  const shown = projectActiveSteerPrefix(settled, { continuationIndex: rows.length, continuation })
  assert.equal(shown[0], settled[0], 'unrelated history must keep its identity and content')
  assert.equal(shown[0].markdown_range, undefined)
  assert.equal(shown[0].blocks?.[0].markdown_range, undefined)
  assert.match(message(shown[2]), /<strong>Plan<\/strong>/)
  assert.match(message(continuation), /<strong>ned<\/strong> maintenance/)
  assert.equal(JSON.stringify(rows), original)
})

test('repeated ID-less active steers format every predecessor through its position', () => {
  const rows = [assistant('**3. Don'), steer, assistant('**3. Don’t'), steer]
  const next = assistant('**3. Don’t continue**')
  rows.forEach(row => { delete row.id })
  delete next.id
  const continuation = projectSteerContinuationMessage(rows[2], next, { active: true })
  const shown = projectActiveSteerPrefix(projectSettledSteerContinuations(rows), { continuationIndex: rows.length, continuation })
  const html = [...shown, continuation].map(message).join('')
  assert.match(html, /<strong>3\. Don<\/strong>/)
  assert.match(html, /<strong>’t<\/strong>/)
  assert.match(html, /<strong> continue<\/strong>/)
  assert.doesNotMatch(html, /\*\*/)
})
