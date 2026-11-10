import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { Marked } from 'marked'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { mathTokens } from '../markdown/mathTokens.js'

const vite = await createSsrRenderServer()
const { default: InlineContent } = await vite.ssrLoadModule(
  '/src/components/ChatView/markdown/InlineContent.jsx',
)
const { BlockToken } = await vite.ssrLoadModule(
  '/src/components/ChatView/markdown/blocks.jsx',
)
const { StandardMarkdown, ProgressiveMarkdown } = await vite.ssrLoadModule(
  '/src/components/ChatView/markdown/BlockRenderer.jsx',
)

after(() => vite.close())

test('standalone HTML comments render no visible block', () => {
  const md = new Marked()
  const [comment, , paragraph] = md.lexer('<!-- internal note -->\n\nVisible text')

  assert.equal(comment.type, 'html')
  assert.equal(renderToStaticMarkup(React.createElement(BlockToken, { token: comment })), '')
  assert.match(
    renderToStaticMarkup(React.createElement(BlockToken, { token: paragraph })),
    />Visible text<\//,
  )
})

test('escaped currency renders its dollar sign while real math stays math', () => {
  const md = new Marked()
  md.use(mathTokens())
  const [paragraph] = md.lexer('Revenue reached \\$100M while $x$ stays math.')

  assert.deepEqual(
    paragraph.tokens.map(token => [token.type, token.text]),
    [
      ['text', 'Revenue reached '],
      ['escape', '$'],
      ['text', '100M while '],
      ['inlineKatex', 'x'],
      ['text', ' stays math.'],
    ],
  )
  const markup = renderToStaticMarkup(
    React.createElement(InlineContent, { tokens: paragraph.tokens }),
  )
  assert.match(markup, /Revenue reached \$100M while/)
  assert.doesNotMatch(markup, /\\\$100M/)
  assert.match(markup, /md-math-inline/)
})

test('app blocks render only where the caller opts in, including nested containers', () => {
  const priorWindow = globalThis.window
  globalThis.window = { location: new URL('https://mobius.test/shell') }
  try {
    const fence = '```mobius-app\n' + JSON.stringify({ app: 'example', intent: 'open:a', title: 'Fix typo',
      action: { label: 'Send', intent: 'send:a' } }) + '\n```'
    const render = (Component, props) => renderToStaticMarkup(React.createElement(QueryClientProvider,
      { client: new QueryClient() }, React.createElement(Component, { text: fence, ...props })))
    for (const Component of [StandardMarkdown, ProgressiveMarkdown]) {
      const plain = render(Component, {})
      assert.match(plain, /<pre[^]*&quot;app&quot;:&quot;example&quot;/)
      assert.doesNotMatch(plain, /md-app-block/)
      const owned = render(Component, { allowAppBlocks: true })
      assert.match(owned, /class="md-app-block"/)
      assert.doesNotMatch(owned, /<pre/)
    }
    for (const nested of ['> ' + fence.split('\n').join('\n> '), '- item\n\n  ' + fence.split('\n').join('\n  ')]) {
      assert.doesNotMatch(render(StandardMarkdown, { text: nested }), /md-app-block/)
      assert.match(render(StandardMarkdown, { text: nested, allowAppBlocks: true }), /md-app-block/)
    }
  } finally {
    globalThis.window = priorWindow
  }
})

test('a server-rendered table keeps its scroll wrapper and never draws the scrollbar', () => {
  const md = new Marked()
  const [table] = md.lexer('| Name | Id |\n| --- | --- |\n| Build | `run-2026-11-10` |')

  const html = renderToStaticMarkup(React.createElement(BlockToken, { token: table }))

  assert.match(html, /<div class="md-table-wrap"><table class="md-table">/)
  assert.match(html, /<td><code class="md-inline-code">run-2026-11-10<\/code><\/td>/)
  assert.doesNotMatch(html, /md-table-scrollbar/)
})
