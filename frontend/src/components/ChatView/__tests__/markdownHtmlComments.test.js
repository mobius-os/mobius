import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Marked } from 'marked'

const here = dirname(fileURLToPath(import.meta.url))

test('standalone HTML comments are treated as suppressible block HTML', () => {
  const md = new Marked()
  const tokens = md.lexer('<!-- internal note -->\n\nVisible text')

  assert.equal(tokens[0].type, 'html')

  const blocksSource = readFileSync(resolve(here, '../markdown/blocks.jsx'), 'utf8')
  assert.match(
    blocksSource,
    /case 'html': return null/,
    'block HTML tokens must not fall through to raw visible text',
  )
})
