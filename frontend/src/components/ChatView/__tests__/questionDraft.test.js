import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  clearChatQuestionDrafts,
  clearQuestionDraft,
  questionDraftKey,
  readQuestionDraft,
  writeQuestionDraft,
} from '../questionDraft.js'


class MemoryStorage {
  constructor() { this.values = new Map() }
  get length() { return this.values.size }
  getItem(key) { return this.values.get(key) ?? null }
  setItem(key, value) { this.values.set(key, String(value)) }
  removeItem(key) { this.values.delete(key) }
  key(index) { return [...this.values.keys()][index] ?? null }
}


const questions = [{
  question: 'Which direction?',
  multiSelect: true,
  options: [{ label: 'Polish' }, { label: 'Simplify' }],
}]


test('question drafts restore selections and custom text by chat and question', () => {
  const storage = new MemoryStorage()
  const key = questionDraftKey('chat-1', 'question-7', questions)

  writeQuestionDraft(
    key,
    { 'Which direction?': ['Polish', '__other__'] },
    { 'Which direction?': 'Keep the current shape' },
    storage,
  )

  assert.deepEqual(readQuestionDraft(key, storage), {
    answers: { 'Which direction?': ['Polish', '__other__'] },
    otherTexts: { 'Which direction?': 'Keep the current shape' },
  })
  assert.deepEqual(
    readQuestionDraft(questionDraftKey('chat-2', 'question-7', questions), storage),
    { answers: {}, otherTexts: {} },
    'another chat must not inherit the selection',
  )
})


test('legacy questions without an id get a stable content-derived draft key', () => {
  const first = questionDraftKey('chat-1', null, questions)
  const second = questionDraftKey('chat-1', null, structuredClone(questions))
  assert.equal(first, second)
  assert.notEqual(first, questionDraftKey('chat-1', null, [{ question: 'Different?' }]))
})


test('question drafts clear on submit and with the owning chat', () => {
  const storage = new MemoryStorage()
  const first = questionDraftKey('chat-1', 'q1', questions)
  const second = questionDraftKey('chat-1', 'q2', questions)
  const otherChat = questionDraftKey('chat-2', 'q1', questions)
  for (const key of [first, second, otherChat]) {
    writeQuestionDraft(key, { 'Which direction?': 'Polish' }, {}, storage)
  }

  clearQuestionDraft(first, storage)
  assert.equal(storage.getItem(first), null)
  clearChatQuestionDrafts('chat-1', storage)
  assert.equal(storage.getItem(second), null)
  assert.notEqual(storage.getItem(otherChat), null)
})


test('durable browser storage falls back when localStorage rejects writes', () => {
  const localDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  const sessionDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const session = new MemoryStorage()
  const blockedLocal = {
    getItem() { return null },
    setItem() { throw new Error('blocked') },
    removeItem() {},
    get length() { return 0 },
    key() { return null },
  }
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true, value: blockedLocal,
  })
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true, value: session,
  })
  try {
    const key = questionDraftKey('chat-1', 'fallback', questions)
    writeQuestionDraft(key, { 'Which direction?': 'Simplify' }, {})
    assert.equal(JSON.parse(session.getItem(key)).answers['Which direction?'], 'Simplify')
  } finally {
    if (localDescriptor) Object.defineProperty(globalThis, 'localStorage', localDescriptor)
    else delete globalThis.localStorage
    if (sessionDescriptor) Object.defineProperty(globalThis, 'sessionStorage', sessionDescriptor)
    else delete globalThis.sessionStorage
  }
})


test('a durable write removes a stale session fallback', () => {
  const localDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  const sessionDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const local = new MemoryStorage()
  const session = new MemoryStorage()
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: local })
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: session })
  try {
    const key = questionDraftKey('chat-1', 'current', questions)
    session.setItem(key, JSON.stringify({
      answers: { 'Which direction?': 'Polish' }, otherTexts: {},
    }))

    writeQuestionDraft(key, { 'Which direction?': 'Simplify' }, {})

    assert.equal(session.getItem(key), null)
    assert.equal(readQuestionDraft(key).answers['Which direction?'], 'Simplify')
  } finally {
    if (localDescriptor) Object.defineProperty(globalThis, 'localStorage', localDescriptor)
    else delete globalThis.localStorage
    if (sessionDescriptor) Object.defineProperty(globalThis, 'sessionStorage', sessionDescriptor)
    else delete globalThis.sessionStorage
  }
})


test('clearing a draft removes every fallback copy', () => {
  const localDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  const sessionDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const local = new MemoryStorage()
  const session = new MemoryStorage()
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: local })
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: session })
  try {
    const key = questionDraftKey('chat-1', 'clear', questions)
    const value = JSON.stringify({ answers: { 'Which direction?': 'Polish' }, otherTexts: {} })
    local.setItem(key, value)
    session.setItem(key, value)

    writeQuestionDraft(key, {}, {})

    assert.equal(local.getItem(key), null)
    assert.equal(session.getItem(key), null)
    assert.deepEqual(readQuestionDraft(key), { answers: {}, otherTexts: {} })
  } finally {
    if (localDescriptor) Object.defineProperty(globalThis, 'localStorage', localDescriptor)
    else delete globalThis.localStorage
    if (sessionDescriptor) Object.defineProperty(globalThis, 'sessionStorage', sessionDescriptor)
    else delete globalThis.sessionStorage
  }
})


test('legacy session drafts migrate once into durable storage', () => {
  const localDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  const sessionDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const local = new MemoryStorage()
  const session = new MemoryStorage()
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: local })
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: session })
  try {
    const key = questionDraftKey('chat-1', 'legacy', questions)
    session.setItem(key, JSON.stringify({
      answers: { 'Which direction?': 'Polish' }, otherTexts: {},
    }))

    assert.equal(readQuestionDraft(key).answers['Which direction?'], 'Polish')
    assert.equal(session.getItem(key), null)
    assert.equal(JSON.parse(local.getItem(key)).version, 1)
  } finally {
    if (localDescriptor) Object.defineProperty(globalThis, 'localStorage', localDescriptor)
    else delete globalThis.localStorage
    if (sessionDescriptor) Object.defineProperty(globalThis, 'sessionStorage', sessionDescriptor)
    else delete globalThis.sessionStorage
  }
})
