import test from 'node:test'
import assert from 'node:assert/strict'
import {
  messageSources,
  safeSourceUrl,
  sourceHost,
  sourceLabel,
} from '../messageSources.js'

const tool = sources => ({ type: 'tool', tool: 'WebSearch', sources })

test('collects sources across every tool block in the message', () => {
  const blocks = [
    { type: 'text', content: 'answer' },
    tool([{ title: 'A', url: 'https://a.example/1' }]),
    { type: 'thinking', content: '...' },
    tool([{ title: 'B', url: 'https://b.example/2' }]),
  ]
  assert.deepEqual(messageSources(blocks).map(s => s.title), ['A', 'B'])
})

test('dedupes across searches, first occurrence wins so search order is kept', () => {
  const blocks = [
    tool([{ title: 'First', url: 'https://x.example/p' }]),
    tool([
      { title: 'Duplicate', url: 'https://x.example/p' },
      { title: 'Second', url: 'https://y.example/q' },
    ]),
  ]
  assert.deepEqual(messageSources(blocks).map(s => s.title), ['First', 'Second'])
})

test('a turn with no web search yields nothing (ordinary replies unchanged)', () => {
  assert.deepEqual(messageSources([{ type: 'text', content: 'hi' }]), [])
  assert.deepEqual(messageSources([tool([])]), [])
  assert.deepEqual(messageSources([{ type: 'tool', tool: 'Bash' }]), [])
})

test('malformed rows never reach an href', () => {
  const blocks = [tool([
    { title: 'no url' },
    { title: 'null url', url: null },
    { title: 'object url', url: { evil: true } },
    { title: 'empty', url: '' },
    { title: 'no host', url: 'https://' },
    { title: 'bad host', url: 'https://exa mple.com/x' },
    { title: 'good', url: 'https://ok.example/z' },
  ])]
  assert.deepEqual(messageSources(blocks).map(s => s.title), ['good'])
})

test('valid URLs are trimmed before rendering and deduping', () => {
  const sources = messageSources([tool([
    { title: 'trimmed', url: '  https://ok.example/z  ' },
    { title: 'duplicate', url: 'https://ok.example/z' },
  ])])
  assert.deepEqual(sources, [{ title: 'trimmed', url: 'https://ok.example/z' }])
  assert.equal(safeSourceUrl('  HTTPS://ok.example/z  '), 'HTTPS://ok.example/z')
})

// The chips now render unconditionally at the end of every answer rather than
// behind a disclosure, so the scheme is re-checked here instead of trusting the
// two runner call sites to stay correct forever. A `javascript:` string is a
// perfectly ordinary non-empty string, so an emptiness check is not a guard.
test('only http(s) URLs are collected — no javascript:/data:/mailto:', () => {
  const blocks = [tool([
    { title: 'xss', url: 'javascript:alert(1)' },
    { title: 'data', url: 'data:text/html,<script>alert(1)</script>' },
    { title: 'mail', url: 'mailto:a@b.com' },
    { title: 'protocol-relative', url: '//evil.example/x' },
    { title: 'plain http', url: 'http://ok.example/a' },
    { title: 'https', url: 'https://ok.example/b' },
  ])]
  assert.deepEqual(messageSources(blocks).map(s => s.title),
    ['plain http', 'https'])
})

test('non-array input is tolerated (a message with no blocks)', () => {
  assert.deepEqual(messageSources(undefined), [])
  assert.deepEqual(messageSources(null), [])
})

test('sourceHost returns the host, and empty string for an unparseable URL', () => {
  assert.equal(sourceHost('https://www.example.com/a/b'), 'www.example.com')
  assert.equal(sourceHost('not a url'), '')
})

// Codex's WebSearchThreadItem exposes a URL only on its openPage/findInPage
// actions and never a title, so the title-less shape is the Codex reality —
// not a hypothetical.
test('a title-less source (Codex) reads as its host, not the raw URL', () => {
  assert.equal(sourceLabel({ url: 'https://nodejs.org/en/blog/release/v24.0.0' }),
    'nodejs.org')
  assert.equal(sourceLabel({ title: '', url: 'https://a.example/p' }), 'a.example')
})

test('a title equal to the URL is not treated as a real title', () => {
  const url = 'https://a.example/p'
  assert.equal(sourceLabel({ title: url, url }), 'a.example')
})

test('a real title (Claude) wins over the host', () => {
  assert.equal(sourceLabel({ title: 'Node.js Releases', url: 'https://nodejs.org/x' }),
    'Node.js Releases')
})

test('an unparseable URL still yields a usable label', () => {
  assert.equal(sourceLabel({ url: 'not a url' }), 'not a url')
})
