import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createServer } from 'vite'
import { createFileDragHandlers } from '../dragUpload.js'
import { questionAnswerPatch, questionAnswersReady } from '../questionSubmission.js'
import { questionDraftKey, writeQuestionDraft, readQuestionDraft } from '../questionDraft.js'

// Exercise the component's event handlers and actual upload hook without a DOM.
// Only these two modules use the existing hook harness; children remain React
// elements so their props describe the actual card's presentation boundary.
const hooksPath = '/src/components/ChatView/hooks/__tests__/react-hook-shim.mjs'
const vite = await createServer({
  appType: 'custom', logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
  plugins: [{
    name: 'question-card-hooks', enforce: 'pre',
    transform(source, id) {
      if (/\/(QuestionCard\.jsx|useFileUpload\.js)$/.test(id)) {
        return source.replaceAll("from 'react'", "from 'question-card-hooks'")
      }
    },
    resolveId(id) {
      if (id === 'question-card-hooks') return '\0question-card-hooks'
    },
    load(id) {
      if (id === '\0question-card-hooks') return `export * from '${hooksPath}'; export const useContext = () => globalThis.__questionLocalAnswers || null;`
    },
  }],
})
const { renderHook } = await vite.ssrLoadModule(hooksPath)
const { default: QuestionCard, CustomAnswerArea } = await vite.ssrLoadModule('/src/components/ChatView/QuestionCard.jsx')
after(() => vite.close())

function elements(node) {
  if (!node || typeof node !== 'object') return []
  if (Array.isArray(node)) return node.flatMap(elements)
  return [node, ...elements(node.props?.children)]
}
function find(tree, predicate) {
  const element = elements(tree).find(predicate)
  assert.ok(element, 'expected rendered control')
  return element
}
const submit = tree => find(tree, node => node.props?.className === 'qcard__submit')
const editor = (tree, question) => find(tree, node => node.props?.question === question)
const chipSets = tree => elements(tree).filter(node => Array.isArray(node.props?.files))
const chips = (tree, index = 0) => chipSets(tree)[index]
const sentSets = tree => elements(tree).filter(node => Array.isArray(node.props?.attachments))
const tick = () => new Promise(resolve => setImmediate(resolve))
const questions = [
  { question: 'First?', options: [{ label: 'A' }] },
  { question: 'Second?', multiSelect: true, options: [{ label: 'B' }] },
]

test('pasting into question two keeps the file with that answer, upload gates submit, and submission locks all attachment edits', async () => {
  const originalFetch = globalThis.fetch
  let finishUpload
  let finishAnswer
  let posted
  let uploads = 0
  globalThis.fetch = () => {
    uploads += 1
    return new Promise(resolve => { finishUpload = resolve })
  }
  const card = renderHook(QuestionCard, {
    chatId: 'interaction-chat', questionId: 'interaction-card', questions,
    onAnswer: (...args) => { posted = args; return new Promise(resolve => { finishAnswer = resolve }) },
  })
  try {
    assert.equal(submit(card.result.current).props.disabled, true)
    const file = new File(['notes'], 'notes.txt', { type: 'text/plain' })
    const uploading = editor(card.result.current, 'Second?').props.onPasteFiles([file])
    assert.equal(uploads, 1)
    assert.equal(chips(card.result.current, 0).props.files.length, 0)
    assert.equal(chips(card.result.current, 1).props.files.length, 1)
    assert.equal(submit(card.result.current).props.disabled, true)
    assert.equal(editor(card.result.current, 'Second?').props.canSubmit, false)
    editor(card.result.current, 'Second?').props.onSubmitShortcut(null)
    assert.equal(posted, undefined)
    assert.equal(elements(card.result.current).filter(node => node.props?.['aria-label'] === 'Files for this answer').length, 2)
    finishUpload({ ok: true, json: async () => [{ name: 'notes.txt', size: 5, mime_type: 'text/plain' }] })
    await uploading
    // The file answers question two only; question one still needs its own.
    assert.equal(submit(card.result.current).props.disabled, true)
    editor(card.result.current, 'First?').props.onChange('first answer')
    assert.equal(submit(card.result.current).props.disabled, false)
    submit(card.result.current).props.onClick({ currentTarget: { closest: () => null } })
    assert.equal(posted[1]['First?'], 'first answer')
    assert.equal(posted[1]['Second?'], 'Attached 1 file')
    assert.equal(posted[0], '- First?: first answer\n- Second?: Attached 1 file\n  Files: notes.txt')
    assert.deepEqual(posted[3].attachments, [{ name: 'notes.txt', size: 5, mime_type: 'text/plain', question: 'Second?' }])
    assert.equal(submit(card.result.current).props.disabled, true)
    assert.equal(chips(card.result.current, 1).props.disabled, true)
    assert.equal(editor(card.result.current, 'Second?').props.onPasteFiles, undefined)
    assert.equal(find(card.result.current, node => node.props?.type === 'file').props.disabled, true)
    finishAnswer(true)
    await tick()
    assert.equal(submit(card.result.current).props.children, 'Submitted')
    assert.deepEqual(sentSets(card.result.current).map(set => set.props.attachments.map(f => f.name)), [[], ['notes.txt']])
  } finally { card.unmount(); globalThis.fetch = originalFetch }
})

test('all drops belong to the visible chat overlay, never hidden card geometry', () => {
  const card = renderHook(QuestionCard, { chatId: 'drop-chat', questionId: 'drop-card', questions })
  try {
    assert.equal(card.result.current.props.onDrop, undefined)
    const attached = []
    let depth = 1
    let active = true
    const handlers = createFileDragHandlers({
      getDepth: () => depth, setDepth: value => { depth = value },
      setActive: value => { active = value }, onFiles: files => attached.push(...files),
    })
    const file = { name: 'notes.txt' }
    handlers.onDrop({ dataTransfer: { types: ['Files'], files: [file] }, preventDefault() {}, stopPropagation() {} })
    assert.deepEqual(attached, [file])
    assert.equal(depth, 0)
    assert.equal(active, false)
    assert.equal(chips(card.result.current).props.files.length, 0)
  } finally { card.unmount() }
})

test('a streamed answer receipt keeps card-level attachments for cross-tab rendering', () => {
  const receipt = questionAnswerPatch({ 'First?': 'Attached 1 file' }, {
    attachments: [{ name: 'notes.txt', size: 5, mime_type: 'text/plain' }],
  })
  assert.deepEqual(receipt.attachments, [{ name: 'notes.txt', size: 5, mime_type: 'text/plain' }])
})

test('rejected and locally queued submissions retain the draft for retry', async () => {
  for (const outcome of [false, { status: 'locally_queued' }, { status: 'locally_settled' }]) {
    const card = renderHook(QuestionCard, {
      chatId: 'retry-chat', questionId: 'retry-card', questions,
      onAnswer: async () => outcome,
    })
    try {
      editor(card.result.current, 'First?').props.onChange('first answer')
      editor(card.result.current, 'Second?').props.onChange('second answer')
      submit(card.result.current).props.onClick({ currentTarget: { closest: () => null } })
      await tick()
      assert.equal(submit(card.result.current).props.children, 'Submit')
      assert.equal(submit(card.result.current).props.disabled, false)
      assert.equal(editor(card.result.current, 'First?').props.value, 'first answer')
    } finally { card.unmount() }
  }
})

test('a failed upload does not block submit, and remote settlement safely discards unsent files', async () => {
  const originalFetch = globalThis.fetch
  const calls = []
  const props = { chatId: 'remote-chat', questionId: 'remote-card', questions }
  const card = renderHook(QuestionCard, props)
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return options.method === 'DELETE' ? { ok: true } : { ok: false, text: async () => 'Failed' }
  }
  try {
    editor(card.result.current, 'First?').props.onChange('first answer')
    editor(card.result.current, 'Second?').props.onChange('second answer')
    await editor(card.result.current, 'Second?').props.onPasteFiles([new File(['a'], 'a.txt')])
    assert.equal(chips(card.result.current, 1).props.files[0].status, 'error')
    assert.equal(submit(card.result.current).props.disabled, false)
    chips(card.result.current, 1).props.onRemove(chips(card.result.current, 1).props.files[0].id)
    globalThis.fetch = async (url, options) => {
      calls.push({ url, options })
      return { ok: true, json: async () => [{ name: 'unsent.txt', size: 1, mime_type: 'text/plain' }] }
    }
    await editor(card.result.current, 'Second?').props.onPasteFiles([new File(['a'], 'a.txt')])
    card.rerender({ ...props, answeredMap: { 'First?': 'Remote', 'Second?': 'Remote' }, attachments: [] })
    assert.equal(calls.filter(call => call.options.method === 'DELETE').length, 1)
    assert.equal(submit(card.result.current).props.children, 'Submitted')
  } finally { card.unmount(); globalThis.fetch = originalFetch }
})


test('files answer a single-question card for empty single, multi-select and other answers', () => {
  const single = questions.slice(0, 1)
  for (const answer of [undefined, [], '__other__', ['__other__']]) {
    const selected = { 'First?': answer }
    assert.equal(questionAnswersReady(single, selected, {}, []), false)
    assert.equal(questionAnswersReady(single, selected, {}, [{ name: 'answer.txt', group: 'First?' }]), true)
    assert.equal(questionAnswersReady(single, selected, {}, [{ name: 'legacy.txt' }]), true)
  }
})


test('files answer only the question they were attached to', () => {
  const file = [{ name: 'answer.txt', group: 'Second?' }]
  assert.equal(questionAnswersReady(questions, {}, {}, file), false)
  assert.equal(questionAnswersReady(questions, { 'Second?': 'No' }, {}, file), false)
  assert.equal(questionAnswersReady(questions, { 'First?': 'Yes' }, {}, file), true)
  assert.equal(questionAnswersReady(questions, { 'First?': 'Yes' }, {}, [{ name: 'legacy.txt' }]), false)
})


test('legacy card-level files stay visible outside answer boxes after submission', () => {
  const card = renderHook(QuestionCard, {
    chatId: 'legacy-chat', questionId: 'legacy-card', questions,
    answeredMap: { 'First?': 'A', 'Second?': 'B' },
    attachments: [{ name: 'old.txt', size: 3, mime_type: 'text/plain' }],
  })
  try {
    assert.deepEqual(sentSets(card.result.current).map(set => set.props.attachments.map(f => f.name)), [[], [], ['old.txt']])
  } finally { card.unmount() }
})

test('restored shared draft files cannot answer a blank grouped question, but remain shared when sent', async () => {
  const storage = {
    values: new Map(),
    getItem(key) { return this.values.get(key) || null },
    setItem(key, value) { this.values.set(key, value) },
    removeItem(key) { this.values.delete(key) },
  }
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })
  const key = questionDraftKey('legacy-draft', 'legacy-q', questions)
  writeQuestionDraft(key, { answers: { 'First?': 'A' }, otherTexts: {}, files: [
    { name: 'shared.txt', size: 1, mime_type: 'text/plain', status: 'done' },
  ] }, storage)
  let posted
  const card = renderHook(QuestionCard, { chatId: 'legacy-draft', questionId: 'legacy-q', questions,
    onAnswer: async (...args) => { posted = args; return true } })
  try {
    assert.equal(submit(card.result.current).props.disabled, true)
    assert.deepEqual(chipSets(card.result.current).map(set => set.props.files.map(f => f.name)), [[], [], ['shared.txt']])
    assert.equal(readQuestionDraft(key, storage).files[0].group, undefined)
    editor(card.result.current, 'Second?').props.onChange('B')
    assert.equal(submit(card.result.current).props.disabled, false)
    submit(card.result.current).props.onClick({ currentTarget: { closest: () => null } })
    await tick()
    assert.equal(posted[1]['Second?'], 'B')
    assert.deepEqual(posted[3].attachments, [{ name: 'shared.txt', size: 1, mime_type: 'text/plain' }])
    assert.deepEqual(sentSets(card.result.current).map(set => set.props.attachments.map(f => f.name)), [[], [], ['shared.txt']])
  } finally {
    card.unmount()
    if (original) Object.defineProperty(globalThis, 'localStorage', original)
    else delete globalThis.localStorage
  }
})

test('queued legacy card-level files remain shared after reload', () => {
  globalThis.__questionLocalAnswers = [{
    chatId: 'legacy-queue',
    body: { question_id: 'legacy-q', answers: { 'First?': 'A', 'Second?': 'B' },
      attachments: [{ name: 'queued.txt', size: 1, mime_type: 'text/plain' }] },
  }]
  const card = renderHook(QuestionCard, { chatId: 'legacy-queue', questionId: 'legacy-q', questions })
  try {
    assert.deepEqual(sentSets(card.result.current).map(set => set.props.attachments.map(f => f.name)), [[], [], ['queued.txt']])
    assert.equal(submit(card.result.current).props.disabled, true)
  } finally { card.unmount(); delete globalThis.__questionLocalAnswers }
})

test('the model-facing answer prose names a reused upload under each tagged question', async () => {
  const storage = {
    values: new Map(),
    getItem(key) { return this.values.get(key) || null },
    setItem(key, value) { this.values.set(key, value) },
    removeItem(key) { this.values.delete(key) },
  }
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })
  const key = questionDraftKey('same-upload', 'same-card', questions)
  writeQuestionDraft(key, { answers: {}, otherTexts: {}, files: [
    { name: 'same.txt', group: 'First?', size: 1, mime_type: 'text/plain', status: 'done' },
    { name: 'same.txt', group: 'Second?', size: 1, mime_type: 'text/plain', status: 'done' },
  ] }, storage)
  let posted
  const card = renderHook(QuestionCard, { chatId: 'same-upload', questionId: 'same-card', questions,
    onAnswer: async (...args) => { posted = args; return true } })
  try {
    assert.equal(submit(card.result.current).props.disabled, false)
    submit(card.result.current).props.onClick({ currentTarget: { closest: () => null } })
    await tick()
    assert.equal(posted[0], '- First?: Attached 1 file\n  Files: same.txt\n- Second?: Attached 1 file\n  Files: same.txt')
    assert.deepEqual(posted[3].attachments.map(file => file.question), ['First?', 'Second?'])
  } finally {
    card.unmount()
    if (original) Object.defineProperty(globalThis, 'localStorage', original)
    else delete globalThis.localStorage
  }
})


test('answer paste matches the composer: Markdown by default, plain on the shortcut, never once answered', () => {
  const clipboard = {
    files: [], items: [],
    getData: type => ({ 'text/plain': 'hi', 'text/markdown': '**hi**' })[type] || '',
  }
  const paste = props => {
    const changes = []
    const box = renderHook(CustomAnswerArea, {
      question: 'Q?', value: 'ab', canSubmit: false, onSubmitShortcut: () => {},
      onChange: value => changes.push(value), ...props,
    })
    let prevented = false
    try {
      const textarea = box.result.current
      if (props.plainShortcut) textarea.props.onKeyDown({ key: 'v', shiftKey: true, metaKey: true })
      textarea.props.onPaste({
        clipboardData: clipboard,
        preventDefault: () => { prevented = true },
        currentTarget: { selectionStart: 1, selectionEnd: 1 },
      })
    } finally { box.unmount() }
    return { changes, prevented }
  }

  assert.deepEqual(paste({}), { changes: ['a**hi**b'], prevented: true })
  assert.deepEqual(paste({ plainShortcut: true }), { changes: ['ahib'], prevented: true })
  assert.deepEqual(paste({ answered: true }), { changes: [], prevented: false })
})


test('an answer takes at most 20 files and says so when more are attached', async () => {
  const originalFetch = globalThis.fetch
  let uploads = 0
  globalThis.fetch = (url, options) => {
    if (options?.method === 'POST') uploads += 1
    return new Promise(() => {})
  }
  const card = renderHook(QuestionCard, {
    chatId: 'limit-chat', questionId: 'limit-card', questions: questions.slice(0, 1),
  })
  try {
    const many = Array.from({ length: 21 }, (_, i) => new File(['x'], `f${i}.txt`))
    editor(card.result.current, 'First?').props.onPasteFiles(many)
    assert.equal(chips(card.result.current).props.files.length, 20)
    assert.equal(uploads, 1, 'uploads run one at a time')
    const error = find(card.result.current, node => node.props?.className === 'qcard__submit-error')
    assert.equal(error.props.children, 'Attach at most 20 files to one card.')
  } finally { card.unmount(); globalThis.fetch = originalFetch }
})
