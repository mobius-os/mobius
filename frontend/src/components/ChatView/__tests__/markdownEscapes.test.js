import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Marked } from 'marked'
import { mathTokens } from '../markdown/mathTokens.js'

const here = dirname(fileURLToPath(import.meta.url))

test('escaped currency bypasses math and renders without its backslash', () => {
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

  const inlineSource = readFileSync(resolve(here, '../markdown/InlineContent.jsx'), 'utf8')
  assert.match(
    inlineSource,
    /if \(token\.type === 'escape'\) \{\s*return token\.text\s*\}/,
    'escape tokens must render their parsed text rather than raw Markdown',
  )
})
