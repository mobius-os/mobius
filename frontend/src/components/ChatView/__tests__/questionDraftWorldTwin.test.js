/* The workspace keeps a chat mounted in both worlds (Standard and Builder), so
   one question card has a hidden twin. Text typed into the card on screen must
   be what the twin shows when the owner flips worlds, and the hidden twin's
   stale state must never overwrite that draft. */
import { after, afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { act } from 'react'
import { h, mount, unmountAll } from './mountedRender.js'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
after(() => vite.close())
const { default: QuestionCard } = await vite.ssrLoadModule('/src/components/ChatView/QuestionCard.jsx')
const { ChatSurfaceHiddenContext } = await vite.ssrLoadModule('/src/components/ChatView/chatSurfaceHiddenContext.js')

class MemoryStorage {
  constructor() { this.values = new Map() }
  get length() { return this.values.size }
  getItem(key) { return this.values.get(key) ?? null }
  setItem(key, value) { this.values.set(key, String(value)) }
  removeItem(key) { this.values.delete(key) }
  key(index) { return [...this.values.keys()][index] ?? null }
}
let storageDescriptor
beforeEach(() => {
  storageDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
})
afterEach(() => {
  unmountAll()
  if (storageDescriptor) Object.defineProperty(globalThis, 'localStorage', storageDescriptor)
  else delete globalThis.localStorage
})

const QUESTIONS = [{ header: 'Pick', question: 'Which one?', options: [{ label: 'A', description: 'First' }] }]

function worlds({ standardHidden }) {
  const card = hidden => h(ChatSurfaceHiddenContext.Provider, { value: hidden },
    h('section', { 'data-world': hidden ? 'hidden' : 'shown' },
      h(QuestionCard, {
        chatId: 'chat-1',
        questionId: 'q-1',
        questions: QUESTIONS,
        onAnswer: () => {},
      })))
  return h('div', null, card(standardHidden), card(!standardHidden))
}

const answerField = (view, index) => view.container.querySelectorAll('textarea')[index]
function type(textarea, text) {
  const propsKey = Object.keys(textarea).find(key => key.startsWith('__reactProps$'))
  act(() => { textarea[propsKey].onChange({ target: { value: text } }) })
}

test('text typed in one world shows in the other after a flip', () => {
  // Both copies mount before any typing, as a retained Builder world does.
  const view = mount(worlds({ standardHidden: false }))
  type(answerField(view, 0), 'my half-typed answer')

  view.rerender(worlds({ standardHidden: true }))
  assert.equal(answerField(view, 1).value, 'my half-typed answer',
    'the copy now on screen shows the draft typed in the other world')

  type(answerField(view, 1), 'edited in Builder')
  view.rerender(worlds({ standardHidden: false }))
  assert.equal(answerField(view, 0).value, 'edited in Builder',
    'returning shows the edit made in the other world')
})

test('a hidden copy re-rendering never overwrites the draft', () => {
  const view = mount(worlds({ standardHidden: false }))
  type(answerField(view, 0), 'keep me')
  // Any re-render of the hidden twin (new props, a stream update) must
  // not save its stale, empty input over the owner's draft.
  view.rerender(worlds({ standardHidden: false }))
  view.rerender(worlds({ standardHidden: true }))
  view.rerender(worlds({ standardHidden: false }))
  assert.equal(answerField(view, 0).value, 'keep me')
  assert.equal(answerField(view, 1).value, 'keep me')
})

test('an upload in flight when its copy is hidden stays with that copy', async t => {
  const posts = []
  t.mock.method(globalThis, 'fetch', (url, options = {}) => {
    if (url.endsWith('/upload-limits')) return Promise.resolve({ ok: true, json: async () => ({ max_bytes: 1 << 20 }) })
    if (options.method === 'POST') return new Promise(resolve => posts.push(resolve))
    return Promise.resolve({ ok: true })
  })
  const view = mount(worlds({ standardHidden: false }))
  const input = view.container.querySelectorAll('input')[0]
  const propsKey = Object.keys(input).find(key => key.startsWith('__reactProps$'))
  act(() => { input[propsKey].onChange({ target: { files: [new File(['abc'], 'notes.txt', { type: 'text/plain' })], value: '' } }) })
  await act(async () => { await new Promise(resolve => setImmediate(resolve)) })
  assert.equal(posts.length, 1, 'the upload is in flight')

  // Switch worlds mid-upload; the upload finishes while this copy is hidden.
  view.rerender(worlds({ standardHidden: true }))
  await act(async () => {
    posts[0]({ ok: true, json: async () => [{ name: 'notes.txt', size: 3, mime_type: 'text/plain' }] })
    await new Promise(resolve => setImmediate(resolve))
  })
  view.rerender(worlds({ standardHidden: false }))
  const shown = view.container.querySelectorAll('section')[0]
  assert.match(shown.textContent, /notes/, 'back in its own world, the attachment is still there')
})

test('revealing a copy never saves its stale input, even briefly', () => {
  const view = mount(worlds({ standardHidden: false }))
  type(answerField(view, 0), 'typed in Standard')
  const writes = []
  const store = globalThis.localStorage
  const setItem = store.setItem.bind(store)
  const removeItem = store.removeItem.bind(store)
  store.setItem = (key, value) => { writes.push(['set', key, value]); setItem(key, value) }
  store.removeItem = key => { writes.push(['remove', key]); removeItem(key) }

  view.rerender(worlds({ standardHidden: true }))
  const draftWrites = writes.filter(([, key]) => key.startsWith('qa-draft:'))
  assert.ok(draftWrites.every(([op, , value]) => op === 'set' && value.includes('typed in Standard')),
    `every save during the switch keeps the owner's text: ${JSON.stringify(draftWrites)}`)
})
