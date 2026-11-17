/* A block control that disappears under keyboard focus leaves focus in the
   block, never on the page (markdown/blockFocus.js). */
import assert from 'node:assert/strict'
import test from 'node:test'
import { createFocusKeeper } from '../../markdown/blockFocus.js'

function element(name) {
  return { name, isConnected: true, disabled: false, attrs: {}, children: [], parent: null,
    hasAttribute(key) { return key in this.attrs }, setAttribute(key, value) { this.attrs[key] = value },
    focus(options) { doc.activeElement = this; doc.calls.push([this.name, options]) } }
}
let doc
function block({ footer = true } = {}) {
  doc = { body: element('body'), activeElement: null, calls: [] }
  doc.activeElement = doc.body
  const listeners = {}
  const section = element('section')
  Object.assign(section, {
    footer: footer ? element('footer') : null,
    addEventListener: (type, fn) => { listeners[type] = fn },
    removeEventListener: type => { delete listeners[type] },
    contains(node) { return node === this || node?.parent === this || node?.parent?.parent === this },
    querySelector(selector) { assert.equal(selector, ':scope > footer'); return this.footer },
  })
  if (section.footer) section.footer.parent = section
  const control = element('send')
  control.parent = section.footer || section
  const keeper = createFocusKeeper(section, doc)
  const focusIn = node => { doc.activeElement = node; listeners.focusin({ target: node }) }
  const focusOut = (node, relatedTarget = null) => listeners.focusout({ target: node, relatedTarget })
  return { section, control, keeper, focusIn, focusOut, listeners }
}

test('a focused control that vanishes leaves focus on the footer, without scrolling', () => {
  const { section, control, keeper, focusIn } = block()
  focusIn(control)
  control.isConnected = false
  doc.activeElement = doc.body
  keeper.settle()
  assert.deepEqual(doc.calls, [['footer', { preventScroll: true }]])
  assert.equal(section.footer.attrs.tabindex, '-1', 'the footer takes programmatic focus only')
})

test('a control that turns disabled or is removed under focus keeps focus in the block, even when Chrome blurs it first', async () => {
  const { control, keeper, focusIn, focusOut } = block()
  focusIn(control)
  control.disabled = true
  focusOut(control)
  keeper.settle()
  await Promise.resolve()
  assert.equal(doc.activeElement.name, 'footer')
})

test('with no footer (an action hidden while it works) focus waits on the block, then moves to the returning footer', () => {
  const { section, control, keeper, focusIn } = block({ footer: false })
  control.parent = section
  focusIn(control)
  control.isConnected = false
  doc.activeElement = doc.body
  keeper.settle()
  assert.equal(doc.activeElement, section)
  keeper.settle()
  assert.deepEqual(doc.calls.map(([name]) => name), ['section'], 'nothing moves while the block has no footer')
  section.footer = element('footer')
  section.footer.parent = section
  keeper.settle()
  assert.equal(doc.activeElement.name, 'footer', 'Tab then reaches Not now and Confirm')
  assert.deepEqual(doc.calls.map(([, options]) => options), [{ preventScroll: true }, { preventScroll: true }])
})

test('focus the reader moved elsewhere is never taken back', async () => {
  const { control, keeper, focusIn, focusOut } = block()
  focusIn(control)
  const outside = element('composer')
  focusOut(control, outside)
  doc.activeElement = outside
  control.isConnected = false
  keeper.settle()
  assert.deepEqual(doc.calls, [])
  // A click on plain page space blurs to nothing; the control is still there.
  const second = block()
  second.focusIn(second.control)
  second.focusOut(second.control)
  await Promise.resolve()
  doc.activeElement = doc.body
  second.keeper.settle()
  assert.deepEqual(doc.calls, [])
})

test('the keeper stops listening when the block unmounts', () => {
  const { keeper, listeners } = block()
  keeper.dispose()
  assert.deepEqual(Object.keys(listeners), [])
})
