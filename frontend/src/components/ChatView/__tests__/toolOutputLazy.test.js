import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

test('starting a lazy full-output request cannot cancel itself', () => {
  const src = readFileSync(new URL('../ToolBlock.jsx', import.meta.url), 'utf8')
  const start = src.indexOf('useEffect(() => {', src.indexOf('export default function ToolBlock'))
  const end = src.indexOf('// Show the fetched full output', start)
  const effect = src.slice(start, end)

  assert.match(effect, /if \(!open\) \{\s*setLoadingFull\(false\)\s*return\s*\}/,
    'closing the disclosure clears the visible loading state')
  assert.doesNotMatch(effect, /fullOutput !== null \|\| loadingFull/,
    'loading state cannot prevent the request it just started from settling')
  const dependencies = effect.match(/\}, \[([^\]]+)\]\)/)?.[1] || ''
  assert.doesNotMatch(dependencies, /\bloadingFull\b/,
    'setting loading state cannot clean up and cancel the active request')
})

test('the live stream forwards reduction metadata into its tool item', () => {
  const src = readFileSync(new URL('../useStreamConnection.js', import.meta.url), 'utf8')
  assert.match(src, /attachToolOutput\(prev, event\.content, event\)/,
    'the reduced excerpt and its lazy-fetch metadata travel together')
})
