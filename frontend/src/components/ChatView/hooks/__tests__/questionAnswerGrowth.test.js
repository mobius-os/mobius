// A Q&A answer typed at the bottom of the chat grows upward, through the real
// scroll owner: the field's pre-edit bottom edge is captured at beforeinput,
// returned to its place by the observer pass that lays the edit out, and
// dropped when it no longer describes the edit being laid out.
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

// The transcript exactly fills a 700px viewport held at the top. The answer
// row ends with a question card whose lower edge rests just above the
// composer (client 604, composer top 620), and the card's answer field sits
// 26px above that edge.
function setup() {
  const timers = new Map()
  const frames = new Map()
  let timerId = 0
  globalThis.setTimeout = (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId }
  globalThis.clearTimeout = id => timers.delete(id)
  globalThis.requestAnimationFrame = (callback) => { frames.set(++timerId, callback); return timerId }
  globalThis.cancelAnimationFrame = id => frames.delete(id)
  globalThis.window = new EventTarget()
  globalThis.document = Object.assign(new EventTarget(), { activeElement: null, visibilityState: 'visible' })
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  globalThis.getComputedStyle = () => ({ paddingTop: '8px', paddingBottom: '96px', zoom: '1' })
  let deliverResize
  globalThis.ResizeObserver = class {
    constructor(callback) { deliverResize = callback }
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  globalThis.MutationObserver = class { observe() {} disconnect() {} }
  const spacer = { style: { height: '0px' }, get offsetHeight() { return parseFloat(this.style.height) } }
  const user = { offsetTop: 40, offsetHeight: 40, dataset: { key: 'u', cid: 'user' }, children: [] }
  const answer = { offsetTop: 104, offsetHeight: 500, dataset: { key: 'a' }, children: [] }
  const list = { get offsetHeight() { return answer.offsetTop + answer.offsetHeight + 96 } }
  const foot = { offsetHeight: 80, getBoundingClientRect: () => ({ top: 620, bottom: 700 }) }
  const chat = Object.assign(new EventTarget(), { style: { setProperty() {} } })
  const handlers = {}
  const scroll = {
    scrollTop: 0, clientHeight: 700, clientWidth: 1000, offsetWidth: 1000,
    offsetHeight: 700, currentCSSZoom: 1, dataset: {},
    addEventListener(type, handler) { (handlers[type] ||= []).push(handler) },
    removeEventListener(type, handler) {
      handlers[type] = (handlers[type] || []).filter(entry => entry !== handler)
    },
    contains: target => target === editor,
    getBoundingClientRect: () => ({ left: 0, top: 0, bottom: 700, width: 1000, height: 700 }),
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
  }
  Object.defineProperty(scroll, 'scrollHeight', { get: () => list.offsetHeight + spacer.offsetHeight })
  const cardBottom = () => answer.offsetTop + answer.offsetHeight - scroll.scrollTop
  const card = { getBoundingClientRect: () => ({ top: cardBottom() - 300, bottom: cardBottom() }) }
  const field = { height: 40 }
  const editor = {
    dataset: { chatInlineEditor: 'question-answer' },
    closest: selector => (selector === '[data-question-id]' ? card : null),
    getBoundingClientRect: () => {
      const bottom = cardBottom() - 26
      return { top: bottom - field.height, bottom, height: field.height }
    },
  }
  globalThis.document.activeElement = editor
  const messages = [{ role: 'user', cid: 'user', content: 'Prompt' }, { role: 'assistant', id: 'a', content: 'Answer' }]
  const args = {
    chatId: 'answer-growth', scrollRef: { current: scroll }, spacerRef: { current: spacer },
    lastUserMsgRef: { current: user }, chatRef: { current: chat }, footRef: { current: foot },
    messages, messagesRef: { current: messages }, initialEntryPhase: 'ready', ownsReadingPosition: true,
  }
  hook = renderHook(value => useScrollMode(value), args)
  const reveal = [...timers.values()].find(timer => timer.delay === 50)
  assert.ok(reveal, 'entry layout installed its quiet reveal')
  reveal.callback()
  assert.equal(hook.result.current.revealed, true)
  assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT')

  const fire = type => (handlers[type] || []).forEach(handler => handler({ type, target: editor }))
  const observeEditor = () => deliverResize([{ target: editor }])
  const flushFrames = () => {
    while (frames.size) {
      const [id, callback] = frames.entries().next().value
      frames.delete(id)
      callback()
    }
  }
  // The field gains one 20px line, and its row with it.
  const growLine = () => {
    field.height += 20
    answer.offsetHeight += 20
  }
  // Content above the conversation's rows grew (a late renderer).
  const shiftAbove = (px) => {
    user.offsetTop += px
    answer.offsetTop += px
  }
  fire('focusin')
  observeEditor() // the observer's first report of the focused field
  assert.equal(scroll.scrollTop, 0, 'an observation with no edit behind it moves nothing')
  return { scroll, cardBottom, fire, observeEditor, flushFrames, growLine, shiftAbove }
}

test('an edit at the chat bottom holds the card edge and moves the conversation up', () => {
  const { scroll, cardBottom, fire, observeEditor, flushFrames, growLine } = setup()
  fire('beforeinput')
  growLine()
  fire('input')
  observeEditor()
  assert.equal(scroll.scrollTop, 20, 'the conversation moved up by the new line')
  assert.equal(cardBottom(), 604, 'the card kept its place above the composer')
  assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT')
  flushFrames()
  assert.equal(scroll.scrollTop, 20, 'the caret check accepts the held position')
})

test('an anchor repair in the same pass is kept, and only the growth is added', () => {
  const { scroll, cardBottom, fire, observeEditor, growLine, shiftAbove } = setup()
  fire('beforeinput')
  growLine()
  fire('input')
  shiftAbove(50)
  observeEditor()
  assert.equal(scroll.scrollTop, 70, 'the 50px repair plus the 20px line')
  assert.equal(cardBottom(), 604, 'the card kept its place above the composer')
})

test('an edit that did not resize the field lends no position to a later resize', () => {
  const { scroll, fire, observeEditor, flushFrames, growLine } = setup()
  fire('beforeinput')
  fire('input')
  flushFrames() // the edit's frames ran with no resize
  growLine()
  observeEditor()
  assert.equal(scroll.scrollTop, 0, 'a resize with no edit behind it is not held')
})

test('growth laid out under the reader\'s gesture is never held afterwards', () => {
  const { scroll, fire, observeEditor, growLine } = setup()
  fire('beforeinput')
  growLine()
  fire('input')
  hook.result.current.gestureWindowUntilRef.current = Infinity
  observeEditor()
  assert.equal(scroll.scrollTop, 0, 'the reader owns the viewport')
  hook.result.current.gestureWindowUntilRef.current = 0
  observeEditor()
  assert.equal(scroll.scrollTop, 0, 'the deferred growth is not replayed')
})
