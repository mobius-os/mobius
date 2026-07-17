import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const canvas = readFileSync(
  resolve(here, '../../components/AppCanvas/AppCanvas.jsx'),
  'utf8',
)

test('capability sessions are bound to one live app document', () => {
  assert.match(
    canvas,
    /if \(srcVersion !== liveVersionRef\.current\) return[\s\S]*?capabilityHostRef\.current\.handle\(e\.source, msg\)/,
  )
  assert.match(
    canvas,
    /capabilityHostRef\.current\?\.detachSource\?\.\([\s\S]*?framesRef\.current\.get\(v\)\?\.contentWindow/,
  )
  assert.match(
    canvas,
    /if \(loadedDocsRef\.current\.has\(v\)\) \{[\s\S]*?capabilityHostRef\.current\.detachSource/,
  )
  assert.match(canvas, /capabilityHostRef\.current\.destroy\(\)/)
})

test('a visible background pane remains inside the capability boundary', () => {
  assert.match(canvas, /isActive\(\) \{ return visibleRef\.current \}/)
  assert.doesNotMatch(canvas, /isActive\(\) \{ return activeRef\.current \}/)
  assert.match(
    canvas,
    /useLayoutEffect\(\(\) => \{[\s\S]*?if \(visible\) return[\s\S]*?capabilityHostRef\.current\.deactivate\(\)[\s\S]*?\}, \[visible\]\)/,
  )
})

test('AppCanvas exposes only the generic capability wire protocol', () => {
  assert.match(canvas, /moebius:capability-/)
  assert.doesNotMatch(canvas, /moebius:microphone-/)
  assert.doesNotMatch(canvas, /microphoneCaptureRef/)
})
