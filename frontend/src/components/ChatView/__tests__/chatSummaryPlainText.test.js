// Chat notes are agent-to-agent Markdown: `$NAME` is a shell variable, not TeX.
import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

test('chat notes keep dollar text literal while answers still render math', async () => {
  const vite = await createServer({ appType: 'custom', logLevel: 'error',
    server: { middlewareMode: true, hmr: false, ws: false },
    ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
  const hadWindow = 'window' in globalThis
  globalThis.window ??= { location: { href: 'http://localhost/' } }
  try {
    const { StandardMarkdown } = await vite.ssrLoadModule('/src/components/ChatView/markdown/BlockRenderer.jsx')
    const note = 'Log at $TMPDIR/a.log and result at $TMPDIR/b.json.'
    const plain = renderToStaticMarkup(React.createElement(StandardMarkdown, { text: note, math: false }))
    assert.ok(plain.includes('$TMPDIR/a.log and result at $TMPDIR/b.json'))
    const answer = renderToStaticMarkup(React.createElement(StandardMarkdown, { text: note }))
    assert.ok(!answer.includes('$TMPDIR/a.log and result at $TMPDIR/b.json'), 'chat answers keep math support')
  } finally {
    if (!hadWindow) delete globalThis.window
    await vite.close()
  }
})
