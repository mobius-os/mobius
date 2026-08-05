import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { initSwapState, reduceSwap } from '../previewSwapState.js'

const here = dirname(fileURLToPath(import.meta.url))
const canvas = readFileSync(
  resolve(here, '../../components/AppCanvas/AppCanvas.jsx'),
  'utf8',
)
const frame = readFileSync(
  resolve(here, '../../../public/app-frame.html'),
  'utf8',
)

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`)
  assert.notEqual(start, -1, `${name} is present`)
  const open = source.indexOf('{', start)
  let depth = 0
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1
    if (source[index] === '}') depth -= 1
    if (depth === 0) return source.slice(start, index + 1)
  }
  throw new Error(`${name} is incomplete`)
}

function frameMountSignal(version, queue) {
  const source = { version }
  const frameWindow = {
    __frameMounted: false,
    location: { origin: 'https://mobius.test' },
    parent: {
      postMessage(message) { queue.push({ source, version, message }) },
    },
  }
  const createElement = (type, props) => ({ type, props })
  const factory = new Function(
    'window',
    'createElement',
    '_FRAME_APP_ID',
    `${extractFunction(frame, 'signalFrameMounted')}\n`
      + `${extractFunction(frame, 'MountSignal')}\n`
      + 'return MountSignal',
  )
  return { source, mount: factory(frameWindow, createElement, '42') }
}

test('commit ordering keeps capability sessions on the exact live document', () => {
  const queue = []
  const incoming = frameMountSignal('new', queue)
  const marker = incoming.mount()

  // React attaches a host ref during commit before running layout effects.
  marker.props.ref({})
  queue.push({
    source: incoming.source,
    version: 'new',
    message: {
      type: 'moebius:capability-open', requestId: 'layout', capability: 'device.test',
    },
  })
  marker.props.ref(null)
  marker.props.ref({})

  assert.deepEqual(queue.map(event => event.message.type), [
    'moebius:frame-mounted',
    'moebius:capability-open',
  ], 'the stable commit signal fires once and cannot be overtaken or replayed')

  let swap = reduceSwap(initSwapState('old'), { type: 'frame-mounted', version: 'old' })
  swap = reduceSwap(swap, { type: 'version', version: 'new' })
  const handled = []
  function deliver(event) {
    if (event.message.type === 'moebius:frame-mounted') {
      swap = reduceSwap(swap, { type: 'frame-mounted', version: event.version })
    } else if (event.version === swap.liveVersion) {
      handled.push(event)
    }
  }
  queue.forEach(deliver)

  assert.equal(swap.liveVersion, 'new')
  assert.deepEqual(handled.map(event => event.message.requestId), ['layout'])

  deliver({
    source: { version: 'old' },
    version: 'old',
    message: {
      type: 'moebius:capability-open', requestId: 'superseded', capability: 'device.test',
    },
  })
  assert.deepEqual(handled.map(event => event.message.requestId), ['layout'],
    'a superseded frame is ignored rather than retained for later replay')
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
  assert.doesNotMatch(canvas, /deferredCapability|boundedWireBytes/)
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
