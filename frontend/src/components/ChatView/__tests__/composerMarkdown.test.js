/** Rendering is a reversible presentation of exact Markdown, not a rewrite of the draft. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { EditorState } from '@codemirror/state'
import { markdown, markdownLanguage } from '@codemirror/lang-markdown'
import { composerMarkdownDecorations } from '../composerMarkdown.js'
function presentation(text, caret = text.length, options = {}) {
  const state = EditorState.create({ doc: text, selection: { anchor: caret }, extensions: [markdown({ base: markdownLanguage })] })
  const spans = []
  composerMarkdownDecorations(state, options).between(0, text.length, (from, to, value) => spans.push({ from, to, ...value.spec }))
  assert.equal(state.doc.toString(), text)
  return spans
}
function hidden(spans) { return spans.filter(span => span.from < span.to && !span.class && !span.widget) }
test('completed bold, emphasis, code and links render without markers away from the caret', () => {
  const text = '**bold** _italic_ `code` [label](https://example.com)\nnext'
  const spans = presentation(text)
  for (const style of ['cm-md-strong', 'cm-md-emphasis', 'cm-md-code', 'cm-md-link']) assert.ok(spans.some(span => span.class === style), style)
  assert.ok(hidden(spans).some(span => text.slice(span.from, span.to) === 'https://example.com'))
  assert.ok(hidden(spans).some(span => text.slice(span.from, span.to) === '**'))
})
test('editing a construct reveals its source without losing formatted text', () => {
  const spans = presentation('**bold** elsewhere', 4)
  assert.equal(hidden(spans).length, 0)
  assert.ok(spans.some(span => span.class === 'cm-md-strong'))
  assert.equal(spans.filter(span => span.class === 'cm-md-syntax').length, 2)
})
test('composition keeps syntax visible and incomplete Markdown stays editable', () => {
  assert.equal(hidden(presentation('**bold**\nnext', 13, { composing: true })).length, 0)
  assert.equal(hidden(presentation('**unfinished')).length, 0)
})
test('images never create fetchable widgets and raw HTML is just text', () => {
  assert.equal(presentation('![alt](https://example.com/private.png) <img src="https://example.com">\nnext').length, 0)
})
test('headings, lists and fenced code render without hiding line breaks', () => {
  const text = '# Heading\n- **item**\n```js\nlet a = 1\n```\nnext'
  const spans = presentation(text)
  assert.ok(spans.some(span => span.class?.includes('cm-md-heading')))
  assert.ok(spans.some(span => span.widget))
  assert.ok(spans.some(span => span.class?.startsWith('cm-md-code-line')))
  assert.ok(hidden(spans).every(span => !text.slice(span.from, span.to).includes('\n')))
})
test('headings align with prose, with the original Markdown visible when editing', () => {
  const text = '###   Heading\nnext'
  assert.deepEqual(hidden(presentation(text)).map(span => text.slice(span.from, span.to)), ['###   '])
  assert.equal(hidden(presentation(text, text.indexOf('Heading'))).length, 0)
})
test('the draft previews the GitHub mark only for GitHub links, never for text inside code', () => {
  const sources = [
    'https://github.com/owner/repo',
    '<https://github.com/owner/repo>',
    '[label](https://github.com/owner/repo)',
    'https://github.com.example.com/owner/repo',
    'https://github.com@example.com/owner/repo',
    '`https://github.com/owner/repo`',
  ]
  const text = sources.join('\n')
  const marks = presentation(text).filter(span => span.widget)
  assert.deepEqual(marks.map(mark => text.slice(mark.from).split('\n')[0]), sources.slice(0, 3))
  assert.ok(presentation(text).some(span => span.class === 'cm-md-link' && span.from === 0))
  assert.equal(presentation(sources[0], 5).filter(span => span.widget).length, 0)
})
test('code content forms one rounded block without hiding fence rows from the caret', () => {
  const text = '```js\nfirst\n\nlast\n```\nnext'
  const codeLines = (spans) => spans.filter(span => span.class?.startsWith('cm-md-code'))
    .map(span => [text.slice(span.from).split('\n')[0], span.class])
  assert.deepEqual(codeLines(presentation(text)), [
    ['```js', 'cm-md-code-fence'],
    ['first', 'cm-md-code-line cm-md-code-start'],
    ['', 'cm-md-code-line'],
    ['last', 'cm-md-code-line cm-md-code-end'],
    ['```', 'cm-md-code-fence'],
  ])
  assert.deepEqual(codeLines(presentation(text, text.indexOf('first'))).filter(([, style]) => /-start|-end/.test(style)), [
    ['```js', 'cm-md-code-line cm-md-code-start'],
    ['```', 'cm-md-code-line cm-md-code-end'],
  ])
  assert.equal(hidden(presentation(text, text.indexOf('first'))).length, 0)
})
test('blur renders even the construct containing the saved caret', () => {
  assert.equal(hidden(presentation('**bold**', 4, { focused: false })).length, 2)
})
