import assert from 'node:assert/strict'
import test from 'node:test'
import vm from 'node:vm'
import { createPreviewFrame, preparePreviewDocument } from '../../runtime/preview.js'
import * as hooks from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

test('shared preview installs only parent-advertised shortcuts before authored scripts', () => {
  const listeners = new Map()
  const posts = []
  const parent = { postMessage(message) { posts.push(message) } }
  const window = {
    parent,
    document: { addEventListener(type, callback, capture) {
      assert.equal(capture, true)
      listeners.set(type, callback)
    } },
    addEventListener(type, callback) { listeners.set(type, callback) },
  }
  const html = preparePreviewDocument('<!--keep--><!doctype html><script>authored()</script><main>Hello</main>')
  assert.ok(html.startsWith('<!--keep--><!doctype html><script>'))
  const source = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  vm.runInNewContext(source, { window })
  const shortcuts = [{ actionId: 'history.back', binding: { key: ',', mod: true } }]
  const message = (source, value) => listeners.get('message')({
    source, data: { type: 'moebius:frame-shortcuts', shortcuts: value },
  })
  const key = (overrides = {}) => {
    const event = { key: ',', metaKey: true, prevented: false, stopped: false,
      preventDefault() { this.prevented = true },
      stopImmediatePropagation() { this.stopped = true }, ...overrides }
    listeners.get('keydown')(event)
    assert.equal(event.stopped, event.prevented)
    return event.prevented
  }
  assert.equal(posts[0].type, 'moebius:frame-shortcuts-ready')
  message({}, shortcuts)
  assert.equal(key(), false, 'foreign catalogs are ignored')
  message(parent, shortcuts)
  assert.equal(key(), true)
  assert.equal(posts.at(-1).actionId, 'history.back')
  for (const variant of [{ shiftKey: true }, { altKey: true }, { repeat: true }, { isComposing: true }, { key: 'c' }, { metaKey: false }]) {
    assert.equal(key(variant), false, 'text/editor chords stay local')
  }
  message(parent, [{ actionId: 'search.open', binding: { key: 'p', mod: true } }])
  assert.equal(key(), false, 'removed chords stop being captured')
  assert.equal(key({ key: 'p', ctrlKey: true, metaKey: false }), true)
  message(parent, [])
  assert.equal(key({ key: 'p' }), false, 'disconnect releases the catalog')
})

test('preview component owns registration while preserving iframe isolation, refs and load readiness', () => {
  const connections = []
  const retired = []
  const refs = []
  const bridge = { connect(frame) {
    connections.push(frame)
    return () => retired.push(frame)
  } }
  const React = { ...hooks, forwardRef: fn => fn,
    createElement: (type, props) => ({ type, props }) }
  const Preview = createPreviewFrame(bridge, React)
  const frame = {}
  let loads = 0
  const ref = value => refs.push(value)
  const props = { srcDoc: '<!doctype html><p>Hello</p>',
    sandbox: 'allow-scripts allow-popups allow-popups-to-escape-sandbox',
    title: 'Example', onLoad: () => { loads += 1 } }
  const rendered = hooks.renderHook((p, r) => Preview(p, r), props, ref)
  const first = rendered.result.current
  assert.equal(first.type, 'iframe')
  assert.equal(first.props.sandbox, props.sandbox)
  assert.equal(first.props.title, props.title)
  first.props.ref(frame)
  first.props.onLoad()
  assert.equal(loads, 1)
  assert.deepEqual(connections, [frame])
  assert.deepEqual(refs, [frame])
  rendered.rerender({ ...props, className: 'ready' }, ref)
  assert.equal(rendered.result.current.props.srcDoc, first.props.srcDoc, 'readiness does not navigate the child')
  assert.equal(rendered.result.current.props.ref, first.props.ref)
  first.props.ref(null)
  assert.deepEqual(retired, [frame])
  assert.deepEqual(refs, [frame, null])
  const objectRef = { current: null }
  rendered.rerender({ srcDoc: '<p>Next</p>' }, objectRef)
  assert.equal(rendered.result.current.props.sandbox, 'allow-scripts', 'safe default stays opaque')
  assert.notEqual(rendered.result.current.props.srcDoc, first.props.srcDoc)
  rendered.result.current.props.ref(frame)
  assert.equal(objectRef.current, frame)
  rendered.result.current.props.ref(null)
  assert.equal(objectRef.current, null)
  rendered.unmount()
})
