import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { Marked } from 'marked'
import { createServer } from 'vite'
import { mathTokens } from '../markdown/mathTokens.js'

const vite = await createServer({
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  // Inline markdown can reach the shared image-lightbox UI. Bundle its SDK UI
  // dependency because that package intentionally uses extensionless ESM
  // imports which native Node resolution cannot load during SSR.
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { default: InlineContent } = await vite.ssrLoadModule(
  '/src/components/ChatView/markdown/InlineContent.jsx',
)
const { BlockToken } = await vite.ssrLoadModule(
  '/src/components/ChatView/markdown/blocks.jsx',
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
