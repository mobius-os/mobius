import assert from 'node:assert/strict'
import { after, afterEach, beforeEach, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import StaticQuestionCard from '../QuestionCard.jsx'

// A request-list card (block.input_requests) holds independent owner-input
// requests: the owner answers some, the rest stay open and are republished on
// a later card with the same request ids. These tests drive the real
// component's handlers through the existing hook harness, without a DOM.
const hooksPath = '/src/components/ChatView/hooks/__tests__/react-hook-shim.mjs'
const vite = await createSsrRenderServer({
  plugins: [{
    name: 'request-list-card-hooks', enforce: 'pre',
    transform(source, id) {
      if (/\/(QuestionCard\.jsx|useFileUpload\.js)$/.test(id)) {
        return source.replaceAll("from 'react'", "from 'request-list-card-hooks'")
      }
    },
    resolveId(id) {
      if (id === 'request-list-card-hooks') return '\0request-list-card-hooks'
    },
    load(id) {
      if (id === '\0request-list-card-hooks') return `export * from '${hooksPath}'; export const useContext = () => null; export const useId = () => 'qcard';`
    },
  }],
})
const { renderHook } = await vite.ssrLoadModule(hooksPath)
const { default: QuestionCard } = await vite.ssrLoadModule('/src/components/ChatView/QuestionCard.jsx')
after(() => vite.close())

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
  if (storageDescriptor) Object.defineProperty(globalThis, 'localStorage', storageDescriptor)
  else delete globalThis.localStorage
})

function elements(node) {
  if (!node || typeof node !== 'object') return []
  if (Array.isArray(node)) return node.flatMap(elements)
  return [node, ...elements(node.props?.children)]
}
const byClass = (tree, name) => elements(tree)
  .filter(node => node.props?.className?.split?.(' ').includes(name))
const submit = tree => byClass(tree, 'qcard__submit')[0]
const hint = tree => byClass(tree, 'qcard__submit-hint')[0]
// An answer box is named by its question: through the question text it is
// labelled by, or its own label ending with the question.
const editor = (tree, question) => {
  const all = elements(tree)
  const textId = all.find(node => node.props?.className === 'qcard__text'
    && node.props.children?.props?.text === question)?.props.id
  return all.find(node => node.props && 'onChange' in node.props && 'onSubmitShortcut' in node.props
    && ((textId && node.props.questionTextId === textId) || node.props.label?.endsWith(`: ${question}`)))
}
const option = (tree, label) => elements(tree).find(node => (
  node.props?.role && ['radio', 'checkbox'].includes(node.props.role)
  && elements(node.props.children).some(child => child.props?.text === label)
))
const click = node => node.props.onClick({ target: { closest: () => null }, currentTarget: { closest: () => null }, detail: 0 })
const tick = () => new Promise(resolve => setImmediate(resolve))

const deploy = {
  id: 'req-deploy', request_key: 'deploy', request_revision: 1,
  header: 'Deploy', question: 'Deploy to production now?',
  context: 'The staging checks passed an hour ago.',
  options: [{ id: 'yes', label: 'Yes' }, { id: 'later', label: 'Later' }],
}
const regions = {
  id: 'req-regions', request_key: 'regions', request_revision: 1,
  header: 'Regions', question: 'Which regions?', multiSelect: true,
  options: [{ id: 'eu', label: 'EU' }, { id: 'us', label: 'US' }, { id: 'asia', label: 'Asia' }],
}
const notes = {
  id: 'req-notes', request_key: 'notes', request_revision: 1,
  header: 'Notes', question: 'Anything for the release notes?', options: [],
}
const listProps = extra => ({
  chatId: 'list-chat', questionId: 'card-1', inputRequests: true,
  questions: [deploy, regions, notes], ...extra,
})

test('a request shows its context under the prompt as secondary text', () => {
  const html = renderToStaticMarkup(createElement(StaticQuestionCard, listProps()))
  assert.match(html, /class="qcard__text"[^>]*>[\s\S]*?Deploy to production now\?[\s\S]*?<\/div><div class="qcard__context">The staging checks passed an hour ago\.<\/div>/)
  assert.equal((html.match(/class="qcard__context"/g) || []).length, 1, 'a request without context renders none')
  // A list card says nothing its Submit does not, and a question has no title.
  assert.doesNotMatch(html, /qcard__group-copy|Anything you skip|qcard__header|>Deploy<|>Regions<|>Notes</)
})

test('a multi-select request answers with its labels joined and lists their option ids', async () => {
  let posted
  const card = renderHook(QuestionCard, listProps({
    questions: [regions], onAnswer: (...args) => { posted = args; return true },
  }))
  try {
    click(option(card.result.current, 'EU'))
    click(option(card.result.current, 'Asia'))
    assert.equal(option(card.result.current, 'EU').props['aria-checked'], true)
    assert.equal(option(card.result.current, 'US').props['aria-checked'], false)
    click(submit(card.result.current))
    await tick()
    assert.deepEqual(posted[1], { 'Which regions?': 'EU, Asia' })
    assert.deepEqual(posted[3].selected_options, { 'req-regions': ['eu', 'asia'] })
    assert.equal(posted[2], 'card-1')
  } finally { card.unmount() }
})

test('a request-list card submits what is answered and leaves blank requests out of the payload', async () => {
  let posted
  const card = renderHook(QuestionCard, listProps({
    onAnswer: (...args) => { posted = args; return true },
  }))
  try {
    // Nothing answered yet: a tap guides rather than submits.
    assert.equal(submit(card.result.current).props['aria-disabled'], true)
    click(submit(card.result.current))
    assert.equal(posted, undefined)
    assert.equal(hint(card.result.current).props.children, 'Answer at least one question to submit')
    assert.equal(submit(card.result.current).props.children, 'Answer at least one question to submit')

    click(option(card.result.current, 'Yes'))
    assert.equal(submit(card.result.current).props['aria-disabled'], undefined)
    assert.equal(submit(card.result.current).props.disabled, false)
    assert.equal(submit(card.result.current).props.children, 'Submit 1 of 3 answers')
    assert.equal(hint(card.result.current).props.children, '', 'the count is in the button only')
    // Enter in a text box must not send a partial answer by accident.
    assert.equal(editor(card.result.current, 'Anything for the release notes?').props.canSubmit, false)

    click(option(card.result.current, 'US'))
    assert.equal(submit(card.result.current).props.children, 'Submit 2 of 3 answers')
    editor(card.result.current, 'Anything for the release notes?').props.onChange('Nothing')
    assert.equal(submit(card.result.current).props.children, 'Submit', 'every open entry answered')
    editor(card.result.current, 'Anything for the release notes?').props.onChange('')

    click(submit(card.result.current))
    await tick()
    assert.deepEqual(posted[1], {
      'Deploy to production now?': 'Yes',
      'Which regions?': 'US',
    })
    assert.equal(posted[0], '- Deploy to production now?: Yes\n- Which regions?: US')
    assert.deepEqual(posted[3].selected_options, { 'req-deploy': ['yes'], 'req-regions': ['us'] })
    assert.equal(posted[3].closeOnlySelection, false)

    // After submission the card keeps only what was answered on it; the
    // blank request leaves this card and returns on the next one.
    assert.equal(submit(card.result.current).props.children, 'Submitted')
    assert.deepEqual(
      byClass(card.result.current, 'qcard__q').map(node => node.props['data-question-index']),
      [0, 1],
    )
    assert.equal(editor(card.result.current, 'Anything for the release notes?'), undefined)
    assert.equal(byClass(card.result.current, 'qcard__group-title')[0].props.children, '2 answered')
    assert.equal(byClass(card.result.current, 'qcard__submit-error').length, 0)
    assert.equal(hint(card.result.current).props.children, '')
  } finally { card.unmount() }
})

test('close-only picks never close the conversation while other requests stay open', async () => {
  let posted
  const closable = { ...deploy, options: [{ id: 'skip', label: 'Skip', on_answer: 'close' }] }
  const card = renderHook(QuestionCard, listProps({
    questions: [closable, notes], onAnswer: (...args) => { posted = args; return true },
  }))
  try {
    click(option(card.result.current, 'Skip'))
    click(submit(card.result.current))
    await tick()
    assert.equal(posted[3].closeOnlySelection, false)
  } finally { card.unmount() }
})

test('a republished request restores the owner\'s draft by request id on the newer card', () => {
  const first = renderHook(QuestionCard, listProps({ onAnswer: () => true }))
  try {
    click(option(first.result.current, 'EU'))
    editor(first.result.current, 'Anything for the release notes?').props.onChange('Mention the new regions')
  } finally { first.unmount() }

  // A later turn republishes the same requests (same ids and revisions) in a
  // different order on a new card, next to a new request.
  const fresh = { ...notes, id: 'req-fresh', question: 'Who should review?' }
  const second = renderHook(QuestionCard, listProps({
    questionId: 'card-2', questions: [notes, fresh, regions], onAnswer: () => true,
  }))
  try {
    assert.equal(editor(second.result.current, 'Anything for the release notes?').props.value, 'Mention the new regions')
    assert.equal(editor(second.result.current, 'Who should review?').props.value, '')
    assert.equal(option(second.result.current, 'EU').props['aria-checked'], true)
    assert.equal(byClass(second.result.current, 'qcard__updated').length, 0)
  } finally { second.unmount() }
})

test('a request whose revision changed drops its draft and says it was updated', () => {
  const first = renderHook(QuestionCard, listProps({ onAnswer: () => true }))
  try {
    editor(first.result.current, 'Anything for the release notes?').props.onChange('Old wording answer')
    click(option(first.result.current, 'Later'))
  } finally { first.unmount() }

  const revised = { ...notes, request_revision: 2, question: 'Anything for the changelog?' }
  const props = listProps({ questionId: 'card-2', questions: [deploy, revised], onAnswer: () => true })
  for (const mount of ['republished', 'remounted after switching chats']) {
    const card = renderHook(QuestionCard, props)
    try {
      assert.equal(editor(card.result.current, 'Anything for the changelog?').props.value, '', mount)
      const updated = byClass(card.result.current, 'qcard__updated')
      assert.equal(updated.length, 1, mount)
      assert.equal(updated[0].props.children, 'Updated')
      // The unchanged request keeps its draft.
      assert.equal(option(card.result.current, 'Later').props['aria-checked'], true, mount)
    } finally { card.unmount() }
  }
})

test('a list card with nothing answered draws nothing once it is no longer offered, leaving its successor\'s draft alone', () => {
  const live = renderHook(QuestionCard, listProps({ questionId: 'card-2', onAnswer: () => true }))
  try {
    editor(live.result.current, 'Anything for the release notes?').props.onChange('Draft on the newer card')
  } finally { live.unmount() }
  const before = [...globalThis.localStorage.values.entries()]

  const old = renderHook(QuestionCard, listProps({ questionId: 'card-1', disabled: true }))
  try {
    // The reply draws an "Asked a question" activity row in its place.
    assert.equal(old.result.current, null)
  } finally { old.unmount() }
  assert.deepEqual([...globalThis.localStorage.values.entries()], before)
})

test('a list card first shown inactive restores drafts once it becomes answerable', () => {
  const first = renderHook(QuestionCard, listProps({ onAnswer: () => true }))
  try {
    editor(first.result.current, 'Anything for the release notes?').props.onChange('Kept across the handoff')
  } finally { first.unmount() }
  const card = renderHook(QuestionCard, listProps({ questionId: 'card-2', disabled: true }))
  try {
    assert.equal(editor(card.result.current, 'Anything for the release notes?'), undefined)
    card.rerender(listProps({ questionId: 'card-2', onAnswer: () => true }))
    assert.equal(editor(card.result.current, 'Anything for the release notes?').props.value, 'Kept across the handoff')
  } finally { card.unmount() }
})

test('an ordinary grouped card still needs every answer and keeps its wording', async () => {
  let posted
  const questions = [
    { id: 'q1', question: 'First?', options: [{ id: 'a', label: 'A' }] },
    { id: 'q2', question: 'Second?', options: [] },
  ]
  const card = renderHook(QuestionCard, {
    chatId: 'ordinary-chat', questionId: 'ordinary-card', questions,
    onAnswer: (...args) => { posted = args; return true },
  })
  try {
    click(option(card.result.current, 'A'))
    assert.equal(submit(card.result.current).props['aria-disabled'], true)
    assert.equal(submit(card.result.current).props.children, 'Submit')
    assert.equal(hint(card.result.current).props.children, '')
    click(submit(card.result.current))
    assert.equal(posted, undefined)
    assert.equal(hint(card.result.current).props.children, 'Answer each question before submitting')
    assert.equal(submit(card.result.current).props.children, 'Answer each question before submitting')
    editor(card.result.current, 'Second?').props.onChange('typed')
    assert.equal(submit(card.result.current).props.children, 'Submit')
    click(submit(card.result.current))
    await tick()
    assert.deepEqual(posted[1], { 'First?': 'A', 'Second?': 'typed' })
  } finally { card.unmount() }
  const html = renderToStaticMarkup(createElement(StaticQuestionCard, {
    chatId: 'ordinary-chat', questionId: 'ordinary-html',
    questions: [{ ...deploy, id: 'q3' }, { ...notes, id: 'q4' }],
  }))
  assert.match(html, /Answer each question, then submit them together\./)
  assert.doesNotMatch(html, /qcard__context|qcard__updated/, 'context is a request-list field only')
})
