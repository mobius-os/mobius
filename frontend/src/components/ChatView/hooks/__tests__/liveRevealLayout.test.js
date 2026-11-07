// Committed reveal geometry must follow before the next ResizeObserver delivery.
import { test, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import useScrollMode from '../../useScrollMode.js'

const globals = ['window', 'document', 'localStorage', 'getComputedStyle', 'ResizeObserver', 'MutationObserver', 'requestAnimationFrame', 'cancelAnimationFrame', 'setTimeout', 'clearTimeout']
const original = Object.fromEntries(globals.map(key => [key, globalThis[key]]))
let hook

afterEach(() => {
  hook?.unmount()
  hook = null
  for (const [key, value] of Object.entries(original)) {
    if (value === undefined) delete globalThis[key]
    else globalThis[key] = value
  }
})

function setup() {
  const timers = new Map()
  let timerId = 0
  globalThis.setTimeout = (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId }
  globalThis.clearTimeout = id => timers.delete(id)
  globalThis.requestAnimationFrame = () => ++timerId
  globalThis.cancelAnimationFrame = () => {}
  globalThis.window = new EventTarget()
  globalThis.document = Object.assign(new EventTarget(), { activeElement: null, visibilityState: 'visible' })
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  globalThis.getComputedStyle = () => ({ paddingTop: '8px', paddingBottom: '96px', zoom: '1' })
  globalThis.ResizeObserver = class { observe() {} disconnect() {} }
  globalThis.MutationObserver = class { observe() {} disconnect() {} }
  const spacer = { style: { height: '0px' }, get offsetHeight() { return parseFloat(this.style.height) } }
  const user = { offsetTop: 40, offsetHeight: 40, dataset: { key: 'u', cid: 'user' }, children: [] }
  const answer = { offsetTop: 104, offsetHeight: 200, dataset: { key: 'a' }, children: [] }
  const list = { get offsetHeight() { return answer.offsetTop + answer.offsetHeight + 96 } }
  const foot = { offsetHeight: 80 }
  const chat = Object.assign(new EventTarget(), { style: { setProperty() {} } })
  const scroll = Object.assign(new EventTarget(), {
    scrollTop: 0, clientHeight: 700, clientWidth: 1000, offsetWidth: 1000,
    offsetHeight: 700, currentCSSZoom: 1, dataset: {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 700 }),
    querySelector(selector) {
      if (selector === '.chat__list') return list
      if (selector === '.spacer-dynamic') return spacer
      if (selector === '[data-key="u"]' || selector.includes('data-cid="user"')) return user
      if (selector === '[data-key="a"]') return answer
      return null
    },
    querySelectorAll(selector) {
      if (selector === '.chat__msg[data-key]') return [user, answer]
      if (selector === '.chat__msg--user[data-cid]') return [user]
      return []
    },
  })
  Object.defineProperty(scroll, 'scrollHeight', { get: () => list.offsetHeight + spacer.offsetHeight })
  const messages = [{ role: 'user', cid: 'user', content: 'Prompt' }, { role: 'assistant', id: 'a', content: 'Answer' }]
  const args = {
    chatId: 'live-layout', scrollRef: { current: scroll }, spacerRef: { current: spacer },
    lastUserMsgRef: { current: user }, chatRef: { current: chat }, footRef: { current: foot },
    messages, messagesRef: { current: messages }, initialEntryPhase: 'ready', ownsReadingPosition: true,
  }
  hook = renderHook(value => useScrollMode(value), args)
  // Settle entry explicitly; no observer callback will deliver in these tests.
  const reveal = [...timers.values()].find(timer => timer.delay === 50)
  assert.ok(reveal, 'entry layout installed its quiet reveal')
  reveal.callback()
  assert.equal(hook.result.current.revealed, true)
  return { scroll, spacer, answer, args }
}

test('committed reveal consumes reservation and follows the new tail before resize delivery', () => {
  const { scroll, spacer, answer } = setup()
  hook.result.current.commitSendIntent({ cid: 'user', fallbackWillPin: true })
  assert.equal(scroll.dataset.scrollMode, 'PIN_USER_MSG')
  assert.ok(spacer.offsetHeight > 0)
  answer.offsetHeight = 1000
  hook.result.current.reapplyActiveMode()
  assert.equal(spacer.offsetHeight, 0, 'stale reservation is consumed in this commit')
  assert.equal(scroll.dataset.scrollMode, 'FOLLOW_BOTTOM')
  assert.equal(scroll.scrollTop, scroll.scrollHeight - scroll.clientHeight)
  answer.offsetHeight += 150
  hook.result.current.reapplyActiveMode()
  assert.equal(scroll.scrollTop, scroll.scrollHeight - scroll.clientHeight)
})

test('a live gesture defers committed growth without touching the viewport or reservation', () => {
  const { scroll, spacer, answer } = setup()
  hook.result.current.followLatest()
  const top = scroll.scrollTop
  const room = spacer.offsetHeight
  hook.result.current.gestureWindowUntilRef.current = Infinity
  answer.offsetHeight += 500
  hook.result.current.reapplyActiveMode()
  assert.equal(scroll.scrollTop, top)
  assert.equal(spacer.offsetHeight, room)
  hook.result.current.gestureWindowUntilRef.current = 0
})

test('steady output below a reading hold never manufactures follow', () => {
  const { scroll, answer } = setup()
  const top = scroll.scrollTop
  assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT')
  answer.offsetHeight += 500
  hook.result.current.reapplyActiveMode()
  assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT')
  assert.equal(scroll.scrollTop, top)
})

test('physical touch contact blocks a committed reveal after the timing gate opens', () => {
  const { scroll, spacer, answer } = setup()
  hook.result.current.followLatest()
  const contact = new Event('touchstart')
  Object.defineProperty(contact, 'touches', { value: [{}] })
  scroll.dispatchEvent(contact)
  hook.result.current.gestureWindowUntilRef.current = 0
  const top = scroll.scrollTop
  const room = spacer.offsetHeight
  answer.offsetHeight += 500
  hook.result.current.reapplyActiveMode()
  assert.equal(scroll.scrollTop, top)
  assert.equal(spacer.offsetHeight, room)
  const lift = new Event('touchend')
  Object.defineProperty(lift, 'touches', { value: [] })
  scroll.dispatchEvent(lift)
})

test('a retained inactive chat cannot synchronize or move its old viewport', () => {
  const { scroll, spacer, answer, args } = setup()
  hook.result.current.followLatest()
  hook.rerender({ ...args, ownsReadingPosition: false })
  const top = scroll.scrollTop
  const room = spacer.offsetHeight
  answer.offsetHeight += 500
  hook.result.current.reapplyActiveMode()
  assert.equal(scroll.scrollTop, top)
  assert.equal(spacer.offsetHeight, room)
})
