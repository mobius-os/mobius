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
  assert.match(effect, /const controller = new AbortController\(\)/,
    'each disclosure owns one cancellable request')
  assert.match(effect, /apiFetch\(url, \{ signal: controller\.signal \}\)/,
    'the request receives the disclosure abort signal')
  assert.match(effect, /return \(\) => \{[\s\S]*controller\.abort\(\)[\s\S]*\}/,
    'collapsing or unmounting aborts network work instead of only ignoring it')
})

test('the bounded excerpt stays copyable while the full output loads', () => {
  const src = readFileSync(new URL('../ToolBlock.jsx', import.meta.url), 'utf8')
  const copyStart = src.indexOf('async function copyOutput()')
  const copyEnd = src.indexOf('// The header content', copyStart)
  const copyHandler = src.slice(copyStart, copyEnd)
  assert.doesNotMatch(copyHandler, /loadingFull/,
    'a slow lazy request cannot disable copying the already-rendered excerpt')
  assert.doesNotMatch(src, /disabled=\{loadingFull\}/,
    'the visible Copy excerpt control remains operable during loading')
})

test('the live stream forwards reduction metadata into its tool item', () => {
  const src = readFileSync(new URL('../useStreamConnection.js', import.meta.url), 'utf8')
  assert.match(src, /attachToolOutput\(prev, event\.content, event\)/,
    'the reduced excerpt and its lazy-fetch metadata travel together')
})
