import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { marked } from 'marked'
import {
  assistantClipboardText,
  copyMarkdownSelection,
  insertClipboardText,
  markdownClipboardHtml,
  markdownFromFragment,
  plainTextFromFragment,
  queueClipboardTextUndoably,
  unwrapEnclosingStructure,
} from '../markdownClipboard.js'
import { renderedReplyClipboard } from '../messageCopy.js'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { BlockToken } = await vite.ssrLoadModule('/src/components/ChatView/markdown/blocks.jsx')
after(() => vite.close())

function text(value) {
  return { nodeType: 3, nodeValue: value, textContent: value, parentNode: null }
}

function element(tagName, children = [], attrs = {}, classes = []) {
  const node = {
    nodeType: 1,
    tagName: tagName.toUpperCase(),
    childNodes: children,
    parentNode: null,
    classList: {
      contains: name => classes.includes(name),
      [Symbol.iterator]: function* iterate() { yield* classes },
    },
    getAttribute: name => attrs[name] ?? null,
    querySelector(selector) {
      return this.querySelectorAll(selector)[0] || null
    },
    querySelectorAll(selector) {
      const wanted = selector.toLowerCase()
      const matches = []
      function walk(current) {
        for (const child of current.childNodes || []) {
          if (String(child.tagName || '').toLowerCase() === wanted) matches.push(child)
          walk(child)
        }
      }
      walk(this)
      return matches
    },
  }
  Object.defineProperty(node, 'textContent', {
    get: () => node.childNodes.map(child => child.textContent || '').join(''),
  })
  for (const child of children) child.parentNode = node
  return node
}

function fragment(children = []) {
  const node = { nodeType: 11, childNodes: children, parentNode: null }
  Object.defineProperty(node, 'textContent', {
    get: () => node.childNodes.map(child => child.textContent || '').join(''),
  })
  for (const child of children) child.parentNode = node
  return node
}

function clipboard(values) {
  return { getData: type => values[type] || '' }
}

test('rendered prose serializes to Markdown while plain text drops punctuation', () => {
  const tree = fragment([
    element('h2', [text('Clipboard contract')]),
    element('p', [
      text('Keep '),
      element('strong', [text('structure')]),
      text(' and '),
      element('a', [text('links')], { href: 'https://example.com' }),
      text('.'),
    ]),
    element('ul', [
      element('li', [text('First')]),
      element('li', [text('Second')]),
    ]),
  ])

  assert.equal(markdownFromFragment(tree), [
    '## Clipboard contract',
    '',
    'Keep **structure** and [links](https://example.com).',
    '',
    '- First',
    '- Second',
  ].join('\n'))
  assert.equal(plainTextFromFragment(tree), [
    'Clipboard contract',
    '',
    'Keep structure and links.',
    '',
    '- First',
    '- Second',
  ].join('\n'))
})

test('code serialization preserves inline delimiters and fenced language', () => {
  const inline = element('code', [text('a`b')])
  const code = element('code', [text('const answer = 42\n')], {}, ['language-js'])
  const tree = fragment([
    element('p', [text('Use '), inline, text('.')]),
    element('pre', [code]),
  ])

  assert.equal(
    markdownFromFragment(tree),
    'Use ``a`b``.\n\n```js\nconst answer = 42\n```',
  )
  assert.equal(plainTextFromFragment(tree), 'Use a`b.\n\nconst answer = 42')
})

test('partial plain text cannot turn into Markdown structure after trimming', () => {
  const tree = element('p', [text('# heading > quote - item 1. item <tag> &copy; *x* [y]')])
  assert.equal(
    markdownFromFragment(tree),
    '\\# heading > quote - item 1. item \\<tag> \\&copy; \\*x\\* \\[y\\]',
  )
  assert.equal(markdownFromFragment(element('p', [text('1. first\n- second\n> third')])),
    '1\\. first\n\\- second\n\\> third', 'block markers are escaped at each line start')
})

test('ordinary prose punctuation copies without needless escapes', () => {
  const prose = 'well-known, A & B, snake_case, wow! 3.5 (see step 2) done!'
  assert.equal(markdownFromFragment(element('p', [text(prose)])), prose)
  assert.equal(markdownFromFragment(element('p', [text('_lead and trail_')])),
    '\\_lead and trail\\_', 'an underscore at a word boundary could still mean emphasis')
})

test('copied prose cannot form an image, a table, or a setext heading', () => {
  assert.equal(markdownFromFragment(element('p', [
    text('Wow!'),
    element('a', [text('link')], { href: 'https://example.org' }),
  ])), 'Wow\\![link](https://example.org)')
  assert.equal(markdownFromFragment(element('p', [text('a|b\n:---|---:\nx|y')])),
    'a\\|b\n:---\\|---:\nx\\|y')
  assert.equal(markdownFromFragment(element('p', [text('title\n===')])), 'title\n\\===')

  // The "!" may end a wrapper, or copy-ignored chrome may sit between it and
  // the link: what matters is the serialized adjacency.
  const link = () => element('a', [text('link')], { href: 'https://example.org' })
  assert.equal(markdownFromFragment(element('p', [element('span', [text('Wow!')]), link()])),
    'Wow\\![link](https://example.org)')
  assert.equal(markdownFromFragment(element('p', [
    text('Wow!'), element('button', [text('Copy')]), link(),
  ])), 'Wow\\![link](https://example.org)')
  assert.doesNotMatch(marked.parse(markdownFromFragment(element('p', [
    element('span', [text('!')]), link(),
  ]))), /<img/)
  const slashBang = marked.parse(markdownFromFragment(element('p', [text('a\\!'), link()])))
  assert.doesNotMatch(slashBang, /<img/, 'a literal backslash before "!" does not hide it')
  assert.match(slashBang, /a\\!<a href/)

  const cellWithPipe = element('table', [element('tbody', [
    element('tr', [element('th', [text('a|b')]), element('th', [element('code', [text('c|d')])])]),
  ])])
  assert.equal(markdownFromFragment(cellWithPipe), '| a\\|b | `c\\|d` |\n| --- | --- |',
    'a pipe in a cell is escaped once, whether it came from text or code')
})

test('table cells keep code pipes and their backslashes through a GFM round trip', () => {
  for (const code of ['c|d', 'two\\\\|slashes']) {
    const table = element('table', [element('tbody', [
      element('tr', [element('th', [text('a|b')]), element('th', [element('code', [text(code)])])]),
      element('tr', [element('td', [text('x')]), element('td', [text('y')])]),
    ])])
    const html = marked.parse(markdownFromFragment(table))
    assert.match(html, /<table>/, `${code} keeps the table`)
    assert.ok(html.includes(`<code>${code}</code>`), `${code} keeps its exact code text: ${html}`)
    assert.ok(html.includes('<th>a|b</th>'))
  }
})

test('a selection inside one nested bullet copies just its text', () => {
  // Dragging to the end of a bullet's last line also reaches the start of the
  // next top-level item, so the clone carries both list shells and an empty
  // trailing item.
  const selected = 'Only this nested bullet, with a well-known hyphenated word.'
  const tree = fragment([
    element('ol', [
      element('li', [element('ul', [element('li', [text(selected)])])], { value: '1' }),
      element('li', [text('')], { value: '2' }),
    ], {}, ['md-list']),
  ])
  assert.equal(markdownFromFragment(unwrapEnclosingStructure(tree)), selected)
  assert.equal(plainTextFromFragment(unwrapEnclosingStructure(tree)), selected)

  const cell = fragment([element('table', [element('tbody', [
    element('tr', [element('td', [text('only '), element('strong', [text('this')])])]),
  ])])])
  assert.equal(markdownFromFragment(unwrapEnclosingStructure(cell)), 'only **this**')
  assert.equal(plainTextFromFragment(unwrapEnclosingStructure(cell)), 'only this')
})

test('layout wrappers and copy-ignored chrome do not keep enclosing structure', () => {
  // A drag from inside the last table cell to the start of the next block.
  const tableTail = fragment([
    element('div', [element('table', [element('tbody', [
      element('tr', [element('td', [text('st cell')])]),
    ])])], {}, ['md-table-wrap']),
    element('p', [text('')]),
  ])
  assert.equal(markdownFromFragment(unwrapEnclosingStructure(tableTail)), 'st cell')
  assert.equal(plainTextFromFragment(unwrapEnclosingStructure(tableTail)), 'st cell')

  // A drag from one item to the start of the next item's code block: the
  // cloned language label is chrome, not selected text.
  const codeTail = fragment([element('ol', [
    element('li', [text('One')], { value: '1' }),
    element('li', [element('div', [
      element('pre', [
        element('span', [text('js')], {}, ['md-code-lang']),
        element('code', [text('')], {}, ['language-js']),
      ]),
    ], {}, ['md-code-wrap'])], { value: '2' }),
  ])])
  assert.equal(markdownFromFragment(unwrapEnclosingStructure(codeTail)), 'One')
  assert.equal(plainTextFromFragment(unwrapEnclosingStructure(codeTail)), 'One')
})

test('a whole one-item list or one-cell table keeps its structure', () => {
  const numbered = element('ol', [element('li', [text('Do this')], { value: '1' })])
  const bullet = element('ul', [element('li', [text('note')])])
  const table = element('table', [element('tbody', [
    element('tr', [element('th', [text('only')])]),
  ])])
  assert.equal(markdownFromFragment(numbered), '1. Do this')
  assert.equal(plainTextFromFragment(numbered), '1. Do this')
  assert.equal(markdownFromFragment(bullet), '- note')
  assert.equal(plainTextFromFragment(bullet), '- note')
  assert.equal(markdownFromFragment(table), '| only |\n| --- |')
  assert.equal(plainTextFromFragment(table), 'only')
})

test('whole-reply Copy keeps a one-item list and a one-cell table', () => {
  function reply(blocks) {
    const row = {
      dataset: { key: 'row-1' },
      querySelectorAll: selector => blocks.filter((_, i) => selector === `[data-markdown-block="${i}"]`),
    }
    for (const block of blocks) block.closest = () => row
    return { querySelectorAll: () => [row] }
  }
  const list = element('div', [element('ol', [element('li', [text('Do this')], { value: '1' })])])
  assert.deepEqual(
    renderedReplyClipboard(reply([list]), ['row-1'], [{ message: 0, block: 0, markdown: '1. Do this' }]),
    { markdown: '1. Do this', plainText: '1. Do this' },
  )
  const bullet = element('div', [element('ul', [element('li', [text('note')])])])
  assert.equal(renderedReplyClipboard(reply([bullet]), ['row-1'],
    [{ message: 0, block: 0, markdown: '- note' }]).plainText, '- note')
  const table = element('div', [element('table', [element('tbody', [
    element('tr', [element('th', [text('only')])]),
  ])])], {}, ['md-table-wrap'])
  assert.equal(renderedReplyClipboard(reply([table]), ['row-1'],
    [{ message: 0, block: 0, markdown: '| only |\n| --- |' }]).plainText, 'only')
  // Unwrapped, the table would still read "only": the list cases above are
  // the ones whose marker a wrongly unwrapped whole reply loses.
})

test('line-start escapes apply only at real line starts, not after inline formatting', () => {
  assert.equal(markdownFromFragment(element('p', [
    element('strong', [text('Term')]), text(' - desc'),
  ])), '**Term** - desc')
  assert.equal(markdownFromFragment(element('p', [
    element('code', [text('x')]), text(' # c'),
  ])), '`x` # c')
  assert.equal(markdownFromFragment(element('p', [
    element('strong', [text('Step')]), text(' 2. go'),
  ])), '**Step** 2. go')
  // Real line starts still escape: the paragraph start, after a line break,
  // after a newline inside the text, and digits split from their dot.
  assert.equal(markdownFromFragment(element('p', [text(' - a')])), '\\- a')
  assert.equal(markdownFromFragment(element('p', [
    element('strong', [text('Term')]), element('br'), text('- next'),
  ])), '**Term**  \n\\- next')
  assert.equal(markdownFromFragment(element('p', [
    element('strong', [text('Term')]), text(' a\n# b'),
  ])), '**Term** a\n\\# b')
  assert.equal(markdownFromFragment(element('p', [text('1'), text('. go')])), '1\\. go')
  for (const tree of [
    element('p', [element('strong', [text('Term')]), text(' - desc')]),
    element('p', [element('strong', [text('Step')]), text(' 2. go')]),
  ]) {
    assert.doesNotMatch(marked.parse(markdownFromFragment(tree)), /<(ul|ol|li)/)
  }
})

test('a list rendered from zero keeps its zero', () => {
  const tree = element('ol', [
    element('li', [text('zero')], { value: '0' }),
    element('li', [text('one')], { value: '1' }),
  ])
  assert.equal(markdownFromFragment(tree), '0. zero\n1. one')
})

test('a selection across list items keeps their rendered numbers', () => {
  const tree = fragment([
    element('ol', [
      element('li', [text('Third')], { value: '3' }),
      element('li', [
        text('Fourth'),
        element('ul', [element('li', [text('detail a')]), element('li', [text('detail b')])]),
      ], { value: '4' }),
      element('li', [text(' ')], { value: '5' }),
    ]),
  ])
  assert.equal(markdownFromFragment(tree), [
    '3. Third',
    '4. Fourth',
    '   - detail a',
    '   - detail b',
  ].join('\n'))
  assert.equal(plainTextFromFragment(tree), [
    '3. Third',
    '4. Fourth',
    '   - detail a',
    '   - detail b',
  ].join('\n'))
})

test('rendered ordered list items carry their own number for copy', () => {
  const render = source => renderToStaticMarkup(
    React.createElement(BlockToken, { token: marked.lexer(source)[0] }),
  )
  const ordered = render('3. three\n4. four')
  assert.match(ordered, /<li class="md-list-item" value="3">/)
  assert.match(ordered, /<li class="md-list-item" value="4">/)
  assert.doesNotMatch(render('- bullet'), /value=/)
})

test('a heading ending in a "#" run keeps it through a Markdown round trip', () => {
  for (const [tag, words, html] of [
    ['h2', 'Ticket #', '<h2>Ticket #</h2>'],
    ['h3', 'Use C ##', '<h3>Use C ##</h3>'],
    ['h1', '#', '<h1>#</h1>'],
  ]) {
    const copied = markdownFromFragment(element(tag, [text(words)]))
    assert.equal(marked.parse(copied).trim(), html, copied)
  }
  assert.equal(markdownFromFragment(element('h2', [text('C# notes')])), '## C# notes')
})

test('a marker after a non-breaking or other Unicode space still escapes', () => {
  for (const space of ['\u00a0', '\u2003', ' \u00a0']) {
    for (const marker of ['# x', '- x', '1. x']) {
      const copied = markdownFromFragment(element('p', [text(`${space}${marker}`)]))
      assert.doesNotMatch(marked.parse(copied), /<(h1|ul|ol)/, JSON.stringify(copied))
    }
    const afterBreak = markdownFromFragment(element('p', [
      element('strong', [text('T')]), element('br'), text(space), text('- x'),
    ]))
    assert.doesNotMatch(marked.parse(afterBreak), /<ul/, JSON.stringify(afterBreak))
  }
})

test('composer prefers Markdown unless paste-without-formatting was requested', () => {
  const data = clipboard({
    'text/markdown': '**hello**',
    'text/plain': 'hello',
  })
  assert.equal(assistantClipboardText(data), '**hello**')
  assert.equal(assistantClipboardText(data, true), 'hello')
  assert.equal(assistantClipboardText(clipboard({ 'text/plain': 'ordinary' })), null)
})

test('HTML carrier round-trips Markdown when optional clipboard types are lost', () => {
  const markdown = '## Café\n\n**one & two**'
  const html = markdownClipboardHtml(markdown, 'Café\none & two')
  assert.equal(
    assistantClipboardText(clipboard({ 'text/html': html, 'text/plain': 'Café\none & two' })),
    markdown,
  )
  assert.match(html, /Caf%C3%A9/)
  assert.match(html, /one &amp; two/)
})

test('Markdown insertion preserves the selected caret range', () => {
  assert.deepEqual(
    insertClipboardText('before old after', 7, 10, '**new**'),
    { value: 'before **new** after', caret: 14 },
  )
  assert.deepEqual(
    insertClipboardText('draft', -4, 99, '!'),
    { value: '!', caret: 1 },
    'browser selection offsets are clamped before editing the controlled value',
  )
})

test('Markdown insertion waits until after paste before entering the native undo stack', () => {
  const calls = []
  const queued = []
  const textarea = {
    ownerDocument: {
      defaultView: { queueMicrotask: callback => queued.push(callback) },
      execCommand: (...args) => {
        calls.push(args)
        return true
      },
    },
  }

  assert.equal(queueClipboardTextUndoably(textarea, '**new**'), true)
  assert.deepEqual(calls, [], 'the command must not run inside the cancelled paste event')
  assert.equal(queued.length, 1)
  queued[0]()
  assert.deepEqual(calls, [['insertText', false, '**new**']])
})

test('Markdown insertion can fall back when the browser rejects native editing', () => {
  assert.equal(queueClipboardTextUndoably(null, '**new**'), false)
  assert.equal(queueClipboardTextUndoably({ ownerDocument: {} }, '**new**'), false)

  let rejected = 0
  let runQueued
  assert.equal(queueClipboardTextUndoably({
    ownerDocument: {
      defaultView: { queueMicrotask: callback => { runQueued = callback } },
      execCommand: () => { throw new Error('blocked') },
    },
  }, '**new**', () => { rejected += 1 }), true)
  runQueued()
  assert.equal(rejected, 1)
})

test('a formatting edit selects its text only after the native insert lands', () => {
  for (const accepted of [true, false]) {
    const events = []
    let runQueued
    assert.equal(queueClipboardTextUndoably({
      ownerDocument: {
        defaultView: { queueMicrotask: callback => { runQueued = callback } },
        execCommand: () => { events.push('insert'); return accepted },
      },
    }, '**text**', () => events.push('fallback'), () => events.push('select')), true)
    assert.deepEqual(events, [], 'nothing runs inside the keydown itself')
    runQueued()
    assert.deepEqual(events, accepted ? ['insert', 'select'] : ['insert', 'fallback'])
  }
})

function copySelectionFixture({ endsOutside = false, startsInAttachment = false } = {}) {
  const inside = text('Hello')
  const outsideMessage = text('tail selected in the next message')
  // An attachment name rendered beside the prose, inside the same copy
  // surface but outside every Markdown block.
  const attachmentName = text('report.pdf')
  const overlap = {
    collapsed: false,
    setStart: () => {},
    setEnd: () => {},
    toString: () => 'Hello',
    cloneContents: () => fragment([text('Hello')]),
    commonAncestorContainer: null,
  }
  const doc = {
    createRange: () => ({
      selectNodeContents: () => {},
      cloneRange: () => overlap,
      toString: () => 'Hello',
    }),
  }
  const block = {
    ownerDocument: doc,
    contains: node => node === inside,
  }
  overlap.commonAncestorContainer = block
  const range = {
    startContainer: startsInAttachment ? attachmentName : inside,
    endContainer: endsOutside ? outsideMessage : inside,
    startOffset: 0,
    endOffset: 5,
    intersectsNode: node => node === block,
    toString: () => (startsInAttachment ? 'report.pdf Hello' : 'Hello'),
  }
  doc.getSelection = () => ({ rangeCount: 1, isCollapsed: false, getRangeAt: () => range })
  const root = {
    ownerDocument: doc,
    contains: node => node === inside || node === block || node === attachmentName,
    querySelectorAll: selector => (selector === '[data-markdown-block]' ? [block] : []),
  }
  const written = {}
  const event = {
    currentTarget: root,
    clipboardData: { setData: (type, value) => { written[type] = value } },
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true },
  }
  return { event, written }
}

function listSelectionFixture({ whole }) {
  // A block that is one numbered item, selected whole or only inside it.
  const block = element('ol', [element('li', [text('Do this')], { value: '1' })])
  const overlap = {
    collapsed: false,
    setStart: () => {},
    setEnd: () => {},
    toString: () => (whole ? 'Do this' : 'this'),
    cloneContents: () => fragment([
      element('ol', [element('li', [text(whole ? 'Do this' : 'this')], { value: '1' })]),
    ]),
    commonAncestorContainer: block,
  }
  const doc = {
    createRange: () => ({
      selectNodeContents: () => {},
      cloneRange: () => overlap,
      toString: () => 'Do this',
    }),
  }
  block.ownerDocument = doc
  block.contains = () => false
  const range = {
    startContainer: block,
    endContainer: block,
    intersectsNode: node => node === block,
    toString: () => overlap.toString(),
  }
  doc.getSelection = () => ({ rangeCount: 1, isCollapsed: false, getRangeAt: () => range })
  const root = {
    ownerDocument: doc,
    contains: node => node === block,
    querySelectorAll: selector => (selector === '[data-markdown-block]' ? [block] : []),
  }
  const written = {}
  const event = {
    currentTarget: root,
    clipboardData: { setData: (type, value) => { written[type] = value } },
    preventDefault() {},
  }
  return { event, written }
}

test('a whole one-item list block keeps its marker; a selection inside it does not', () => {
  const whole = listSelectionFixture({ whole: true })
  assert.equal(copyMarkdownSelection(whole.event, () => '1. Do this'), true)
  assert.equal(whole.written['text/markdown'], '1. Do this')
  assert.equal(whole.written['text/plain'], '1. Do this')

  const partial = listSelectionFixture({ whole: false })
  assert.equal(copyMarkdownSelection(partial.event, () => '1. Do this'), true)
  assert.equal(partial.written['text/markdown'], 'this')
  assert.equal(partial.written['text/plain'], 'this')
})

function multiBlockSelectionFixture(parts) {
  // Each part is one rendered block: its full text, the text the selection
  // covers in it, and the clone of that covered range.
  const blocks = parts.map(({ full, selected, clone }) => {
    const block = element('div')
    const overlap = {
      collapsed: false,
      setStart: () => {},
      setEnd: () => {},
      toString: () => selected,
      cloneContents: () => fragment([clone]),
      commonAncestorContainer: block,
    }
    block.ownerDocument = {
      createRange: () => ({
        selectNodeContents: () => {},
        cloneRange: () => overlap,
        toString: () => full,
      }),
    }
    block.contains = () => false
    return block
  })
  const range = {
    startContainer: blocks[0],
    endContainer: blocks[blocks.length - 1],
    intersectsNode: node => blocks.includes(node),
    toString: () => parts.map(part => part.selected).join('\n'),
  }
  const doc = { getSelection: () => ({ rangeCount: 1, isCollapsed: false, getRangeAt: () => range }) }
  const root = {
    ownerDocument: doc,
    contains: node => blocks.includes(node),
    querySelectorAll: selector => (selector === '[data-markdown-block]' ? blocks : []),
  }
  const written = {}
  const event = {
    currentTarget: root,
    clipboardData: { setData: (type, value) => { written[type] = value } },
    preventDefault() {},
  }
  return { event, written, blocks }
}

test('a selection reaching another block keeps the list or table it cuts through', () => {
  // A whole paragraph, then the first item of a two-item list.
  const intoList = multiBlockSelectionFixture([
    { full: 'Steps:', selected: 'Steps:', clone: element('p', [text('Steps:')]) },
    {
      full: 'Do thisThen that',
      selected: 'Do this',
      clone: element('ol', [element('li', [text('Do this')], { value: '1' })]),
    },
  ])
  const sources = new Map([[intoList.blocks[0], 'Steps:']])
  assert.equal(copyMarkdownSelection(intoList.event, block => sources.get(block) || ''), true)
  assert.equal(intoList.written['text/markdown'], 'Steps:\n\n1. Do this')
  assert.equal(intoList.written['text/plain'], 'Steps:\n\n1. Do this')

  // One table cell, then the start of the next paragraph.
  const fromCell = multiBlockSelectionFixture([
    {
      full: 'ab',
      selected: 'b',
      clone: element('table', [element('tbody', [element('tr', [element('td', [text('b')])])])]),
    },
    { full: 'Next words', selected: 'Next', clone: element('p', [text('Next')]) },
  ])
  assert.equal(copyMarkdownSelection(fromCell.event, () => ''), true)
  assert.equal(fromCell.written['text/markdown'], '| b |\n| --- |\n\nNext')
})

test('a selection contained in one message copies its Markdown source', () => {
  const { event, written } = copySelectionFixture({ endsOutside: false })
  assert.equal(copyMarkdownSelection(event, () => '**Hello**'), true)
  assert.equal(event.defaultPrevented, true)
  assert.equal(written['text/markdown'], '**Hello**')
  assert.equal(written['text/plain'], 'Hello')
})

test('a selection reaching into the next message keeps the native copy untruncated', () => {
  const { event, written } = copySelectionFixture({ endsOutside: true })
  assert.equal(copyMarkdownSelection(event, () => '**Hello**'), false)
  assert.equal(event.defaultPrevented, false,
    'overriding here would truncate the visible selection to one message')
  assert.deepEqual(written, {}, 'the native clipboard payload must stay untouched')
})

test('a selection that includes an attachment name keeps the native copy', () => {
  // Both endpoints sit inside the surface, but the attachment text belongs to
  // no Markdown block: serializing only the blocks would silently drop it.
  const { event, written } = copySelectionFixture({ startsInAttachment: true })
  assert.equal(copyMarkdownSelection(event, () => '**Hello**'), false)
  assert.equal(event.defaultPrevented, false,
    'the blocks alone cannot represent the attachment text the user selected')
  assert.deepEqual(written, {}, 'the native clipboard payload must stay untouched')
})

test('chat prose and the composer share the Markdown clipboard boundary', () => {
  const message = readFileSync(new URL('../MsgContent.jsx', import.meta.url), 'utf8')
  const composer = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
  assert.match(message, /onCopy=\{\(event\) => copyMarkdownSelection\(event, markdownForBlock\)\}/)
  assert.match(message, /data-markdown-block=/)
  assert.match(message, /if \(markdownByIndex\) return markdownByIndex\.get\(index\) \?\? ''/,
    'a partial cold render must not fall back to the hidden full message source')
  assert.match(composer, /assistantClipboardText\(\s*e\.clipboardData,\s*preferPlainText/)
  assert.match(composer, /pasteAsPlainTextRef\.current = isPlainTextPasteShortcut\(e\)/)
  assert.match(composer, /if \(queueClipboardTextUndoably\(textarea, pastedText, applyControlledFallback\)\) return/,
    'Markdown paste should enter the browser undo stack before using the controlled fallback')
  assert.match(composer, /pendingComposerCaretRef\.current = next/,
    'paste and sent-message history should share the controlled caret handoff')
  assert.doesNotMatch(composer, /setSelectionRange\(next\.caret/,
    'paste must not introduce a second requestAnimationFrame caret path')
})
