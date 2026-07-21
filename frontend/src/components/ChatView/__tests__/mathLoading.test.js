import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const mathSource = readFileSync(new URL('../markdown/math.js', import.meta.url), 'utf8')
const rendererSource = readFileSync(new URL('../markdown/BlockRenderer.jsx', import.meta.url), 'utf8')
const indexSource = readFileSync(new URL('../../../../index.html', import.meta.url), 'utf8')

test('KaTeX stays off the shell entry path', () => {
  assert.match(mathSource, /import\('katex'\)/)
  assert.doesNotMatch(mathSource, /^import .* from ['"]katex['"]/m)
  assert.doesNotMatch(indexSource, /<script[^>]+katex\.min\.js/)
  assert.doesNotMatch(rendererSource, /marked-katex-extension/)
})
