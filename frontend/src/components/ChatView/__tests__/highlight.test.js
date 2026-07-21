import { test } from 'node:test'
import assert from 'node:assert/strict'

import { highlightCode, highlightSync } from '../markdown/highlight.js'

test('syntax highlighter stays unloaded until the first code block', async () => {
  const code = 'const answer = 42'

  assert.equal(highlightSync(code, 'javascript'), null)

  const highlighted = await highlightCode(code, 'javascript')
  assert.match(highlighted, /hljs-keyword/)
  assert.equal(highlightSync(code, 'javascript'), highlighted)
})
