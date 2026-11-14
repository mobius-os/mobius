/* Q&A formatting shares chat Markdown without changing saved question/choice identity. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

// No DOM in SSR: these fixtures exercise formatting, not DOM sanitization.
globalThis.window = { location: new URL('https://mobius.test/shell/') }
globalThis.location = window.location
const vite = await createSsrRenderServer({ noExternal: ['dompurify'], plugins: [{
  name: 'dompurify-ssr-stub', enforce: 'pre',
  resolveId: id => id === 'dompurify' ? '\0dompurify-stub' : null,
  load: id => id === '\0dompurify-stub' ? 'export default { sanitize: value => String(value) }' : null,
}] })
after(() => vite.close())
const { default: QuestionCard } = await vite.ssrLoadModule('/src/components/ChatView/QuestionCard.jsx')
const question = 'Install **Möbius**?\n\n1. Preserve `local changes`.\n2. Check *Contribute*.'
const label = '**Update** (Recommended)'
const questions = [{ header: '**Approval**', question, options: [{ label,
  description: 'Read [release notes](https://example.com/release).\n\n- Keep **your changes**.',
}, { label: 'Not now' }] }]
const render = props => renderToStaticMarkup(h(QuestionCard, { chatId: 'format', questionId: 'q', questions, ...props }))

test('Q&A headers, prompts and choice explanations render chat Markdown', () => {
  const html = render()
  assert.match(html, /<strong>Approval<\/strong>/)
  assert.match(html, /Install <strong>Möbius<\/strong>/)
  assert.match(html, /<ol class="md-list"/)
  assert.match(html, /class="md-inline-code">local changes<\/code>/)
  assert.match(html, /<em>Contribute<\/em>/)
  assert.match(html, /<strong>Update<\/strong> \(Recommended\)/)
  assert.match(html, /href="https:\/\/example.com\/release"/)
  assert.match(html, /<ul class="md-list"/)
  assert.doesNotMatch(html, /<button[^>]*role="radio"/, 'formatted choices may contain their own links and buttons')
})

test('answered cards still match the original Markdown label, not rendered text', () => {
  const html = render({ answeredMap: { [question]: label } })
  assert.match(html, /role="radio" aria-checked="true" aria-disabled="true"/)
  assert.match(html, /<strong>Update<\/strong>/)
  assert.doesNotMatch(html, /tabindex="0"/)
  assert.match(html, />Submitted</)
})

test('raw HTML does not execute and app-action fences remain inert inside Q&A', () => {
  const html = render({ questions: [{ question: '<script>alert(1)</script>\n\n**Safe**\n\n```mobius-app\n{"app":"example","intent":"send:a","title":"Send"}\n```' }] })
  assert.doesNotMatch(html, /<script|md-app-block/)
  assert.match(html, /<strong>Safe<\/strong>/)
  assert.match(html, /md-code-block/)
})

test('choice labels and headers render inline Markdown only', () => {
  const html = render({ questions: [{ header: '# Plan', question: 'Pick a speed', options: [
    { label: '1. **Fast**' }, { label: '# Careful', description: 'Slower' },
  ] }] })
  assert.match(html, /1\. <strong>Fast<\/strong>/)
  assert.match(html, /# Careful/)
  assert.match(html, /# Plan/)
  assert.doesNotMatch(html, /<ol|<h1/)
})

test('screen-reader names come from the rendered question, not its Markdown source', () => {
  const html = render({ questions: [{ question: 'Use **fast** mode?', options: [{ label: 'Yes' }] },
    { question: 'Anything else?' }] })
  const id = html.match(/class="qcard__text" id="([^"]+)"/)[1]
  assert.match(html, new RegExp(`role="radiogroup" aria-labelledby="${id}"`))
  assert.match(html, new RegExp(`aria-label="Custom answer for:" aria-labelledby="[^" ]+ ${id}"`))
  assert.match(html, new RegExp(`aria-label="Attach a photo or file to your answer to:" aria-labelledby="[^" ]+ ${id}"`))
  assert.doesNotMatch(html, /aria-label[^>]*\*\*/)
})

test('tag-like text stays visible in every Q&A field while raw HTML stays inert', () => {
  const html = render({ questions: [{ header: 'Type <T>', question: 'Pick a return type for <Foo>?\n\n<script>alert(1)</script>', options: [
    { label: 'Return Promise<void>', description: 'Like `Promise<string>` but <b>empty</b>' },
    { label: 'Return Promise<string>' },
  ] }] })
  assert.match(html, /Type &lt;T&gt;/)
  assert.match(html, /Pick a return type for &lt;Foo&gt;\?/)
  assert.match(html, /Return Promise&lt;void&gt;/)
  assert.match(html, /Return Promise&lt;string&gt;/)
  assert.match(html, /&lt;b&gt;empty&lt;\/b&gt;/)
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/)
  assert.doesNotMatch(html, /<script|<b>/)
})

test('a choice control holds only its label; links live in the described explanation', () => {
  const html = render({ questions: [{ question: 'Update?', options: [
    { label: 'See [docs](https://example.com/docs) ![logo](https://example.com/a.png)', description: 'Read [notes](https://example.com/notes).' },
  ] }] })
  const choice = html.match(/<div class="qcard__opt-choice"[^>]*>([\s\S]*?)<\/div>/)
  assert.ok(choice)
  assert.doesNotMatch(choice[1], /<a |<button|<img/)
  assert.match(choice[1], /See docs logo/)
  const describedBy = choice[0].match(/aria-describedby="([^"]+)"/)[1]
  assert.match(html, new RegExp(`<div class="qcard__opt-desc" id="${describedBy}">[\\s\\S]*href="https://example.com/notes"`))
})
