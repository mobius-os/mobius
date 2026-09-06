import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const canvas = readFileSync(
  new URL('../../components/AppCanvas/AppCanvas.jsx', import.meta.url),
  'utf8',
)

test('AppCanvas consumes external account completion before the opaque-frame origin gate', () => {
  const completion = canvas.indexOf('accountLinkCompletion(e, registered)')
  const frameOriginGate = canvas.indexOf(
    "if (e.origin !== 'null' && e.origin !== window.location.origin) return",
  )
  assert.ok(completion >= 0)
  assert.ok(frameOriginGate > completion)
})

test('AppCanvas registration is live-frame, visible, capability and source bound', () => {
  assert.match(canvas, /if \(srcVersion !== liveVersionRef\.current\) return/)
  assert.match(
    canvas,
    /!visibleRef\.current \|\| !identityLinkBrokerAllowed\(capabilityContractRef\.current\)/,
  )
  assert.match(canvas, /source: e\.source,[\s\S]*frameVersion: srcVersion/)
  assert.match(
    canvas,
    /accountLinkRef\.current\?\.source === framesRef\.current\.get\(v\)\?\.contentWindow/,
  )
})

test('AppCanvas forwards only the narrowed result to the exact opaque frame', () => {
  assert.match(canvas, /registered\.source\.postMessage\(completion, '\*'\)/)
  assert.doesNotMatch(canvas, /account-link[\s\S]{0,500}window\.postMessage\([^)]*, '\*'\)/)
})
