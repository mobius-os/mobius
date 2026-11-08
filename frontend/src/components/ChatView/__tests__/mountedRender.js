/* Mount real components with react-dom's client renderer under node:test, so a
   test can drive state transitions (rerender with new props, click a row) and
   observe what stays mounted. Static renders cannot show state loss across a
   remount. The document below implements only the DOM surface react-dom uses
   to build, update and remove plain elements; there is no layout or event
   dispatch, so `click` calls the element's React handler directly. */
import { act, createElement } from 'react'

const HTML = 'http://www.w3.org/1999/xhtml'

class FakeNode {
  constructor(document, nodeType, nodeName) {
    this.ownerDocument = document
    this.nodeType = nodeType
    this.nodeName = nodeName
    this.childNodes = []
    this.parentNode = null
  }
  get firstChild() { return this.childNodes[0] || null }
  get lastChild() { return this.childNodes.at(-1) || null }
  get nextSibling() {
    const siblings = this.parentNode?.childNodes || []
    return siblings[siblings.indexOf(this) + 1] || null
  }
  get textContent() { return this.childNodes.map(child => child.textContent).join('') }
  set textContent(text) {
    for (const child of this.childNodes) child.parentNode = null
    this.childNodes = []
    if (text !== '' && text != null) this.appendChild(this.ownerDocument.createTextNode(String(text)))
  }
  appendChild(child) { return this.insertBefore(child, null) }
  insertBefore(child, before) {
    child.parentNode?.removeChild(child)
    const index = before ? this.childNodes.indexOf(before) : -1
    if (index < 0) this.childNodes.push(child)
    else this.childNodes.splice(index, 0, child)
    child.parentNode = this
    return child
  }
  removeChild(child) {
    this.childNodes.splice(this.childNodes.indexOf(child), 1)
    child.parentNode = null
    return child
  }
  contains(node) {
    for (let current = node; current; current = current.parentNode) if (current === this) return true
    return false
  }
  getRootNode() {
    let current = this
    while (current.parentNode) current = current.parentNode
    return current
  }
  addEventListener() {}
  removeEventListener() {}
}

class FakeText extends FakeNode {
  constructor(document, text) {
    super(document, 3, '#text')
    this.nodeValue = text
  }
  get textContent() { return this.nodeValue }
  set textContent(text) { this.nodeValue = String(text) }
}

class FakeElement extends FakeNode {
  constructor(document, tagName, namespaceURI = HTML) {
    super(document, 1, tagName.toUpperCase())
    this.tagName = this.nodeName
    this.namespaceURI = namespaceURI
    this.attributes = new Map()
    this.style = { setProperty() {}, removeProperty() {} }
    this.scrollTop = 0
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)) }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null }
  hasAttribute(name) { return this.attributes.has(name) }
  removeAttribute(name) { this.attributes.delete(name) }
  get className() { return this.getAttribute('class') || '' }
  matches(selector) {
    const attribute = selector.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/)
    if (attribute) return attribute[2] === undefined ? this.hasAttribute(attribute[1]) : this.getAttribute(attribute[1]) === attribute[2]
    if (selector.startsWith('.')) return this.className.split(/\s+/).includes(selector.slice(1))
    return this.tagName === selector.toUpperCase()
  }
  closest(selector) {
    for (let current = this; current?.nodeType === 1; current = current.parentNode) if (current.matches(selector)) return current
    return null
  }
  querySelectorAll(selector) {
    const found = []
    const visit = node => node.childNodes.forEach(child => {
      if (child.nodeType !== 1) return
      if (child.matches(selector)) found.push(child)
      visit(child)
    })
    visit(this)
    return found
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null }
  focus() { this.ownerDocument.activeElement = this }
  blur() {}
  getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 } }
}

function createDocument() {
  const document = new FakeNode(null, 9, '#document')
  document.ownerDocument = null
  document.createElement = tag => new FakeElement(document, tag)
  document.createElementNS = (namespace, tag) => new FakeElement(document, tag, namespace)
  document.createTextNode = text => new FakeText(document, text)
  document.documentElement = document.appendChild(document.createElement('html'))
  document.body = document.documentElement.appendChild(document.createElement('body'))
  document.activeElement = document.body
  return document
}

// react-dom reads `window` and `document` when it loads, so install them
// before importing it; each node:test file runs in its own process.
const document = createDocument()
const window = { document, event: undefined, HTMLIFrameElement: class {}, addEventListener() {}, removeEventListener() {} }
document.defaultView = window
Object.assign(globalThis, { window, document, IS_REACT_ACT_ENVIRONMENT: true })
const { createRoot } = await import('react-dom/client')

const mounted = new Set()

/** Mount `element` in a fresh container; returns the container and controls. */
export function mount(element, { wrap = node => node } = {}) {
  const container = document.body.appendChild(document.createElement('div'))
  const root = createRoot(container)
  act(() => root.render(wrap(element)))
  const view = {
    container,
    rerender(next) { act(() => root.render(wrap(next))) },
    unmount() {
      if (!mounted.delete(view)) return
      act(() => root.unmount())
      document.body.removeChild(container)
    },
  }
  mounted.add(view)
  return view
}

/** Unmount whatever a failed test left mounted, so its timers stop. */
export function unmountAll() {
  for (const view of [...mounted]) view.unmount()
}

/** Run the element's React click handler, as a tap on it would. */
export function click(element) {
  const propsKey = Object.keys(element).find(key => key.startsWith('__reactProps$'))
  const event = { currentTarget: element, target: element, button: 0, preventDefault() {}, stopPropagation() {} }
  // A handler may be async (a Stop request); its pending promise is the
  // component's business, so act flushes only the synchronous update.
  act(() => { element[propsKey].onClick(event) })
}

export { createElement as h }
