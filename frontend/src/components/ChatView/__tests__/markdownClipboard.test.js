import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  assistantClipboardText,
  insertClipboardText,
  markdownClipboardHtml,
  markdownFromFragment,
  plainTextFromFragment,
  queueClipboardTextUndoably,
} from '../markdownClipboard.js'

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
  const tree = element('p', [text('# heading > quote - item 1. item <tag> &copy;')])
  assert.equal(
    markdownFromFragment(tree),
    '\\# heading \\> quote \\- item 1\\. item \\<tag\\> \\&copy;',
  )
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

test('assistant prose and the composer share the Markdown clipboard boundary', () => {
  const message = readFileSync(new URL('../MsgContent.jsx', import.meta.url), 'utf8')
  const composer = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
  assert.match(message, /onCopy=\{\(event\) => copyAssistantSelection\(event, markdownForBlock\)\}/)
  assert.match(message, /data-assistant-markdown-block=/)
  assert.match(message, /if \(markdownByIndex\) return markdownByIndex\.get\(index\) \?\? ''/,
    'a partial cold render must not fall back to the hidden full message source')
  assert.match(composer, /assistantClipboardText\(\s*e\.clipboardData,\s*preferPlainText/)
  assert.match(composer, /pasteAsPlainTextRef\.current = isPlainTextPasteShortcut\(e\)/)
  assert.match(composer, /if \(queueClipboardTextUndoably\(/,
    'Markdown paste should enter the browser undo stack before using the controlled fallback')
  assert.match(composer, /pendingComposerCaretRef\.current = next/,
    'paste and sent-message history should share the controlled caret handoff')
  assert.doesNotMatch(composer, /setSelectionRange\(next\.caret/,
    'paste must not introduce a second requestAnimationFrame caret path')
})
