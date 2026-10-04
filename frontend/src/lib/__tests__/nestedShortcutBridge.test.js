/* Exercise preview shortcut ownership through the runtime's public connection contract. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { makeShortcuts } from '../../runtime/shortcuts.js'

function harness() {
  const listeners = new Map()
  const posts = []
  const document = {}
  const target = {
    document,
    parent: { postMessage(message) { posts.push(message) } },
    location: { origin: 'https://mobius.test' },
    addEventListener(type, callback) { listeners.set(type, callback) },
    removeEventListener(type) { listeners.delete(type) },
  }
  let bindings = []
  const bridge = makeShortcuts({ getBindings: () => bindings, target })
  const framePosts = []
  const loads = new Map()
  const frame = {
    tagName: 'IFRAME', ownerDocument: document, isConnected: true,
    contentWindow: { postMessage(message) { framePosts.push(message) } },
    addEventListener(type, callback) { loads.set(type, callback) },
    removeEventListener(type) { loads.delete(type) },
  }
  const message = (source, type, actionId, origin = 'null') => listeners.get('message')({ source, origin, data: { type, actionId } })
  const update = value => {
    bindings = value
    message(target.parent, 'moebius:frame-shortcuts', null, target.location.origin)
  }
  return { bridge, update, frame, framePosts, posts, message, loads, listeners, target }
}
const back = [{ actionId: 'history.back', binding: { key: ',', mod: true } }]

test('only an explicitly connected, still-mounted preview can invoke advertised commands', () => {
  const h = harness()
  h.update(back)
  h.message(h.frame.contentWindow, 'moebius:shell-shortcut', 'history.back')
  assert.equal(h.posts.length, 0, 'unregistered child cannot control the shell')
  const disconnect = h.bridge.connect(h.frame)
  h.message({}, 'moebius:shell-shortcut', 'history.back')
  h.message(h.frame.contentWindow, 'moebius:shell-shortcut', 'chat.new')
  assert.equal(h.posts.length, 0, 'source and command must both match')
  h.message(h.frame.contentWindow, 'moebius:shell-shortcut', 'history.back')
  assert.equal(h.posts[0].actionId, 'history.back')
  h.frame.isConnected = false
  h.message(h.frame.contentWindow, 'moebius:shell-shortcut', 'history.back')
  assert.equal(h.posts.length, 1, 'detached preview cannot act')
  h.frame.isConnected = true
  disconnect()
  assert.equal(h.framePosts.at(-1).shortcuts.length, 0)
  assert.equal(h.loads.size, 0)
  h.message(h.frame.contentWindow, 'moebius:shell-shortcut', 'history.back')
  assert.equal(h.posts.length, 1, 'retired preview cannot act')
})

test('registration, child readiness, reload and catalog changes deliver live bindings without navigation', () => {
  const h = harness()
  const disconnect = h.bridge.connect(h.frame)
  assert.equal(h.bridge.connect(h.frame), disconnect, 'registration is idempotent')
  h.update(back)
  h.message(h.frame.contentWindow, 'moebius:frame-shortcuts-ready')
  h.loads.get('load')()
  assert.equal(h.framePosts.length, 4)
  assert.equal(h.framePosts.at(-1).shortcuts[0].actionId, 'history.back')
  h.update([])
  assert.equal(h.framePosts.at(-1).shortcuts.length, 0)
  h.message(h.frame.contentWindow, 'moebius:shell-shortcut', 'history.back')
  assert.equal(h.posts.length, 0, 'removed command cannot be forwarded')
})

test('foreign parent messages and foreign-document frames cannot enlist preview shortcut control', () => {
  const h = harness()
  h.bridge.connect({ ...h.frame, ownerDocument: {} })
  assert.equal(h.framePosts.length, 0)
  h.bridge.connect(h.frame)
  h.message({}, 'moebius:frame-shortcuts')
  h.message(h.target.parent, 'moebius:frame-shortcuts', null, 'https://foreign.test')
  assert.equal(h.framePosts.length, 1)
  h.bridge._destroy()
  assert.equal(h.framePosts.at(-1).shortcuts.length, 0)
  assert.equal(h.listeners.size, 0)
  assert.equal(h.loads.size, 0)
})
