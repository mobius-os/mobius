import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

test('expansion fetches a cancellable bounded preview only after completion', () => {
  const src = readFileSync(new URL('../ToolBlock.jsx', import.meta.url), 'utf8')
  const start = src.indexOf('useEffect(() => {', src.indexOf('export default function ToolBlock'))
  const end = src.indexOf('// Show the larger bounded preview', start)
  const effect = src.slice(start, end)

  assert.match(effect, /if \(!open\) \{\s*setLoadingPreview\(false\)\s*return\s*\}/,
    'closing the disclosure clears the visible loading state')
  assert.match(effect, /if \(t\.status === 'running'\) return/,
    'an intermediate sidecar is never read before the matching tool settles')
  assert.match(effect, /\+ '\?preview=1'/,
    'ordinary expansion requests only the server-bounded preview')
  assert.doesNotMatch(effect, /previewOutput !== null \|\| loadingPreview/,
    'loading state cannot prevent the request it just started from settling')
  const dependencies = effect.match(/\}, \[([^\]]+)\]\)/)?.[1] || ''
  assert.doesNotMatch(dependencies, /\bloadingPreview\b/,
    'setting loading state cannot clean up and cancel the active request')
  assert.match(effect, /const controller = new AbortController\(\)/,
    'each disclosure owns one cancellable request')
  assert.match(effect, /fetchLazyText\(url, \{ signal: controller\.signal \}\)/,
    'the request receives the disclosure abort signal')
  assert.match(effect, /return \(\) => \{[\s\S]*controller\.abort\(\)[\s\S]*\}/,
    'collapsing or unmounting aborts network work instead of only ignoring it')
})

test('explicit copy fetches full output without retaining it in React state', () => {
  const src = readFileSync(new URL('../ToolBlock.jsx', import.meta.url), 'utf8')
  const copyStart = src.indexOf('async function copyOutput()')
  const copyEnd = src.indexOf('function retryPreview()', copyStart)
  const copyHandler = src.slice(copyStart, copyEnd)
  assert.match(copyHandler, /fetchLazyText\(url, \{ signal: controller\.signal \}\)/)
  assert.match(copyHandler, /output = result\.text/)
  assert.doesNotMatch(src, /setFullOutput|\[fullOutput/,
    'the exact potentially-large value never enters persistent component state')
  assert.doesNotMatch(copyHandler, /\?preview=1/,
    'explicit copy requests the exact endpoint rather than the preview')
  assert.match(copyHandler, /!\(previewOutput !== null && previewComplete\)/,
    'a preview known to be complete is reused instead of downloaded twice')
  assert.match(src, /role="status" aria-live="polite"/,
    'copy success and failure are announced independently of the button label')
})

test('the live stream forwards reduction metadata into its tool item', () => {
  const src = readFileSync(new URL('../useStreamConnection.js', import.meta.url), 'utf8')
  assert.match(src, /attachToolOutput\(prev, event\.content, event\)/,
    'the reduced excerpt and its lazy-fetch metadata travel together')
})
