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
    // Tight delimiters still pair as inline math under the single-dollar
    // currency rule (#1811), so this note shows the difference.
    const note = 'Logs went to $TMPDIR/a.log$HOME/b.json.'
    const plain = renderToStaticMarkup(React.createElement(StandardMarkdown, { text: note, math: false }))
    assert.ok(plain.includes('$TMPDIR/a.log$HOME/b.json'))
    const answer = renderToStaticMarkup(React.createElement(StandardMarkdown, { text: note }))
    assert.ok(!answer.includes('$TMPDIR/a.log$HOME/b.json'), 'chat answers keep math support')
  } finally {
    if (!hadWindow) delete globalThis.window
    await vite.close()
  }
})
