/* The chat's one footer copies the agent's whole stretch, even when it began
   before the loaded history page: the missing start is read page by page and
   the whole stretch is projected as the transcript shows it. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { readStretchStart } from '../useStretchPrefix.js'
import { markdownHtml, stretchClipboard, stretchTail } from '../../messageCopy.js'
import { stretchProse as stretchParts } from '../../replyFooter.js'
import { assistantReplyGroups } from '../../assistantReplies.js'
import { projectSettledSteerContinuations } from '../../steerContinuity.js'

// As ChatView reads a stretch whose start was read: the read start and the
// loaded rows grouped together.
const stretchProse = rows => stretchParts([...new Set(assistantReplyGroups(rows).values())])
  .map(part => part.markdown)

const owner = content => ({ role: 'user', content })
const reply = content => ({ role: 'assistant', blocks: [{ type: 'text', content }] })
const carrier = { role: 'user', content: 'answer', hidden: true, kind: 'continuation' }
// One run's rows around a hidden steer: the later row replays the earlier text.
const run = (content, n = 0) => ({ role: 'assistant', id: n ? `run:assistant:${n}` : 'run', content, blocks: [{ type: 'text', content }] })
const steer = { role: 'user', hidden: true, steered: true, source_work_id: 'run', kind: 'peer_message' }

test('a page’s stretch tail runs back past hidden carriers to a visible owner row', () => {
  assert.deepEqual(stretchTail([reply('old'), owner('Go'), reply('a'), carrier, reply('b')]),
    { rows: [reply('a'), carrier, reply('b')], complete: true })
  assert.deepEqual(stretchTail([reply('a'), carrier]), { rows: [reply('a'), carrier], complete: false })
  assert.deepEqual(stretchTail([owner('Go')]), { rows: [], complete: true })
})

test('the stretch’s start is read from earlier pages until its first row, oldest first', async () => {
  const pages = {
    40: { offset: 20, messages: [reply('two'), carrier, reply('three')] },
    20: { offset: 0, messages: [owner('Go'), reply('one'), carrier] },
  }
  const asked = []
  const rows = await readStretchStart(before => { asked.push(before); return pages[before] }, 40)
  assert.deepEqual(asked, [40, 20])
  assert.deepEqual(stretchProse(rows), ['one', 'two', 'three'])
})

test('a page that does not advance fails rather than copy part of the stretch', async () => {
  let calls = 0
  await assert.rejects(readStretchStart(() => {
    calls += 1
    return { offset: 20, messages: [reply('loop')] }
  }, 20), /did not advance/)
  assert.equal(calls, 1, 'the same page is never read again')
})

test('a replayed continuation copies once, within the read start or across its edge', () => {
  // Within the read start alone.
  assert.deepEqual(stretchProse([run('The frame'), steer, run('The framework survives.', 1)]),
    ['The framework survives.'])
  // The replay's first row was read; its continuation was on the loaded page.
  // ChatView passes both together, so the edge does not split the projection.
  const readStart = [reply('Before.'), run('The frame'), steer]
  const loaded = [run('The framework survives.', 1)]
  assert.deepEqual(stretchProse([...readStart, ...loaded]), ['Before.', 'The framework survives.'])
})

test('an exact replay after a visible steer copies only what the transcript shows', () => {
  // The owner steered mid-reply; the continuation re-sent the sealed text.
  // The transcript cuts that replay, so the read start must too, even though
  // the sealed row before the steer belongs to the previous stretch.
  const sealed = { role: 'assistant', id: 'run', content: 'The frame', blocks: [{ type: 'text', content: 'The frame' }] }
  const steered = { role: 'user', steered: true, content: 'Keep going' }
  const continuation = run('The framework survives.', 1)
  const page = [sealed, steered, continuation]
  const shown = projectSettledSteerContinuations(page, { preserveHidden: true })[2]
  assert.notEqual(shown.blocks[0].content, 'The framework survives.', 'the transcript cuts the replay')
  return readStretchStart(() => ({ offset: 0, messages: page }), 3).then(rows => {
    assert.deepEqual(stretchProse(rows), [shown.blocks[0].content.trim()])
  })
})

test('a rendered part reads the screen; one before the loaded page reads its Markdown’s HTML', () => {
  const block = { textContent: 'Result', nodeType: 3, nodeValue: 'Result' }
  const rowElement = { dataset: { key: 'row-1' }, querySelectorAll: selector => (
    selector === '[data-markdown-block="0"]' ? [block] : []) }
  block.closest = () => rowElement
  const list = { querySelectorAll: () => [rowElement] }
  const previous = globalThis.document
  // A template whose content is the HTML's text: enough to show the fallback
  // reads the Markdown's HTML, not its source.
  globalThis.document = { createElement: () => ({
    set innerHTML(html) { this.content = { nodeType: 3, nodeValue: html.replace(/<[^>]+>/g, '') } },
  }) }
  try {
    assert.deepEqual(stretchClipboard([
      { key: 'earlier', block: 0, markdown: '**Before**' },
      { key: 'row-1', block: 0, markdown: '**Result**' },
      { markdown: 'Merge?\n- Yes' },
    ], list), {
      markdown: '**Before**\n\n**Result**\n\nMerge?\n- Yes',
      plainText: 'Before\n\nResult\n\nMerge?\n- Yes',
    })
  } finally {
    globalThis.document = previous
  }
  assert.equal(stretchClipboard([], list), null)
})

test('the read start’s HTML keeps image alt text and app links without the live renderer', () => {
  // The live renderer resolves images in an effect and renders app links as
  // cards needing app context; plain text must not depend on either.
  const image = markdownHtml('![Chart of results](/api/chats/c/media/x.png)')
  assert.match(image, /<img [^>]*alt="Chart of results"/)
  const link = markdownHtml('See [Kanban](/shell/?app=12) and **this**.')
  assert.match(link, /<a href="\/shell\/\?app=12">Kanban<\/a>/)
  assert.match(link, /<strong>this<\/strong>/)
})

test('the read start’s math carries its TeX as the transcript’s KaTeX does', () => {
  assert.match(markdownHtml('Area is $\\pi r^2$ here.'),
    /<span class="katex"><annotation encoding="application\/x-tex">\\pi r\^2<\/annotation><\/span>/)
  assert.match(markdownHtml('$$\nE = mc^2\n$$'),
    /<div class="md-math-block"><annotation encoding="application\/x-tex">E = mc\^2<\/annotation><\/div>/)
  assert.doesNotMatch(markdownHtml('It costs $5 and $10.'), /katex/, 'currency stays prose')
})

test('the read start drops raw HTML as the transcript does, keeping text between inline tags', () => {
  assert.doesNotMatch(markdownHtml('Shown.\n\n<div>Not shown in chat</div>\n\nAlso shown.'), /Not shown/)
  const inline = markdownHtml('Click <button>Continue</button> to proceed.')
  assert.doesNotMatch(inline, /<button/)
  assert.match(inline, /Click Continue to proceed\./)
})
