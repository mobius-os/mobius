import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { createFileDragHandlers } from '../dragUpload.js'
import { questionAnswerPatch, questionAnswersReady } from '../questionSubmission.js'
import { questionDraftKey, writeQuestionDraft } from '../questionDraft.js'

// Exercise the component's event handlers and actual upload hook without a DOM.
// Only these two modules use the existing hook harness; children remain React
// elements so their props describe the actual card's presentation boundary.
const hooksPath = '/src/components/ChatView/hooks/__tests__/react-hook-shim.mjs'
const vite = await createSsrRenderServer({
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
      if (id === '\0question-card-hooks') return `export * from '${hooksPath}'; export const useContext = () => null;`
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
const submit = tree => find(tree, node => node.props?.className?.split(' ').includes('qcard__submit'))
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
  globalThis.fetch = url => {
    if (url.endsWith('/upload-limits')) return Promise.resolve({ ok: true, json: async () => ({ max_bytes: 50 * 1024 * 1024 }) })
    uploads += 1
    return new Promise(resolve => { finishUpload = resolve })
  }
  const card = renderHook(QuestionCard, {
    chatId: 'interaction-chat', questionId: 'interaction-card', questions,
    onAnswer: (...args) => { posted = args; return new Promise(resolve => { finishAnswer = resolve }) },
  })
  try {
    // Incomplete: tappable to find the missing answer, never submittable.
    assert.equal(submit(card.result.current).props.disabled, false)
    assert.equal(submit(card.result.current).props['aria-disabled'], true)
    const hint = tree => elements(tree).find(node => node.props?.className === 'qcard__submit-hint')
    assert.equal(hint(card.result.current), undefined, 'no hint before a Submit attempt')
    submit(card.result.current).props.onClick({ currentTarget: { closest: () => null } })
    assert.equal(posted, undefined, 'an incomplete click guides; it never submits')
    assert.equal(hint(card.result.current)?.props.children, 'Answer each question before submitting')
    assert.equal(submit(card.result.current).props['aria-describedby'], hint(card.result.current).props.id)
    const file = new File(['notes'], 'notes.txt', { type: 'text/plain' })
    const uploading = editor(card.result.current, 'Second?').props.onPasteFiles([file])
    await tick()
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
    assert.equal(submit(card.result.current).props['aria-disabled'], true)
    submit(card.result.current).props.onClick({ currentTarget: { closest: () => null } })
    assert.equal(posted, undefined)
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

test('an oversized answer image stays an error chip without starting an upload', async () => {
  const originalFetch = globalThis.fetch
  let posts = 0
  globalThis.fetch = (url, options) => {
    if (url.endsWith('/upload-limits')) return Promise.resolve({ ok: true, json: async () => ({ max_bytes: 50 * 1024 * 1024 }) })
    if (options?.method === 'POST') posts += 1
    throw new Error('An oversized image must not be posted')
  }
  const card = renderHook(QuestionCard, {
    chatId: 'large-answer-chat', questionId: 'large-answer-card', questions: questions.slice(0, 1),
  })
  try {
    const image = new File(['GIF89a'], 'large.gif', { type: 'image/gif' })
    Object.defineProperty(image, 'size', { value: 50 * 1024 * 1024 + 1 })
    await editor(card.result.current, 'First?').props.onPasteFiles([image])
    assert.equal(posts, 0)
    assert.equal(chips(card.result.current).props.files[0].status, 'error')
  } finally { card.unmount(); globalThis.fetch = originalFetch }
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
    if (url.endsWith('/upload-limits')) return { ok: true, json: async () => ({ max_bytes: 50 * 1024 * 1024 }) }
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


test('legacy untagged files stay card-level on a grouped card after submission', () => {
  const card = renderHook(QuestionCard, {
    chatId: 'legacy-chat', questionId: 'legacy-card', questions,
    answeredMap: { 'First?': 'A', 'Second?': 'B' },
    attachments: [{ name: 'old.txt', size: 3, mime_type: 'text/plain' }],
  })
  try {
    assert.deepEqual(sentSets(card.result.current).map(set => set.props.attachments.map(f => f.name)), [[], [], ['old.txt']])
  } finally { card.unmount() }
})

test('a restored draft keeps each file with its answer and legacy untagged files card-level', async () => {
  const storage = {
    values: new Map(),
    getItem(key) { return this.values.get(key) || null },
    setItem(key, value) { this.values.set(key, value) },
    removeItem(key) { this.values.delete(key) },
  }
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })
  writeQuestionDraft(questionDraftKey('draft-chat', 'draft-q', questions), { answers: { 'First?': 'A' }, otherTexts: {}, files: [
    { name: 'mine.txt', group: 'Second?', size: 1, mime_type: 'text/plain', status: 'done' },
    { name: 'old.txt', size: 1, mime_type: 'text/plain', status: 'done' },
  ] }, storage)
  let posted
  const card = renderHook(QuestionCard, { chatId: 'draft-chat', questionId: 'draft-q', questions,
    onAnswer: async (...args) => { posted = args; return true } })
  try {
    assert.deepEqual(chipSets(card.result.current).map(set => set.props.files.map(f => f.name)), [[], ['mine.txt'], ['old.txt']])
    assert.equal(submit(card.result.current).props.disabled, false)
    submit(card.result.current).props.onClick({ currentTarget: { closest: () => null } })
    await tick()
    assert.equal(posted[0], '- First?: A\n- Second?: Attached 1 file\n  Files: mine.txt')
    assert.deepEqual(posted[3].attachments, [
      { name: 'mine.txt', size: 1, mime_type: 'text/plain', question: 'Second?' },
      { name: 'old.txt', size: 1, mime_type: 'text/plain' },
    ])
  } finally {
    card.unmount()
    if (original) Object.defineProperty(globalThis, 'localStorage', original)
    else delete globalThis.localStorage
  }
})


test('a restored draft file for a question the card no longer asks stays visible and submits card-level', async () => {
  const storage = {
    values: new Map(),
    getItem(key) { return this.values.get(key) || null },
    setItem(key, value) { this.values.set(key, value) },
    removeItem(key) { this.values.delete(key) },
  }
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })
  writeQuestionDraft(questionDraftKey('stale-chat', 'stale-q', questions), { answers: { 'First?': 'A', 'Second?': 'B' }, otherTexts: {}, files: [
    { name: 'gone.txt', group: 'Removed?', size: 1, mime_type: 'text/plain', status: 'done' },
  ] }, storage)
  let posted
  const card = renderHook(QuestionCard, { chatId: 'stale-chat', questionId: 'stale-q', questions,
    onAnswer: async (...args) => { posted = args; return true } })
  try {
    assert.deepEqual(chipSets(card.result.current).map(set => set.props.files.map(f => f.name)), [[], [], ['gone.txt']])
    submit(card.result.current).props.onClick({ currentTarget: { closest: () => null } })
    await tick()
    assert.deepEqual(posted[3].attachments, [{ name: 'gone.txt', size: 1, mime_type: 'text/plain' }])
  } finally {
    card.unmount()
    if (original) Object.defineProperty(globalThis, 'localStorage', original)
    else delete globalThis.localStorage
  }
})

test('a restored draft file for a replaced sole question never answers the new question', async () => {
  const storage = {
    values: new Map(),
    getItem(key) { return this.values.get(key) || null },
    setItem(key, value) { this.values.set(key, value) },
    removeItem(key) { this.values.delete(key) },
  }
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })
  const replaced = [{ question: 'New question?', options: [] }]
  writeQuestionDraft(questionDraftKey('sole-chat', 'sole-q', replaced), { answers: {}, otherTexts: {}, files: [
    { name: 'gone.txt', group: 'Old question?', size: 1, mime_type: 'text/plain', status: 'done' },
  ] }, storage)
  let posted
  const card = renderHook(QuestionCard, { chatId: 'sole-chat', questionId: 'sole-q', questions: replaced,
    onAnswer: async (...args) => { posted = args; return true } })
  try {
    const trays = () => elements(card.result.current).filter(node => node.props?.role === 'group')
      .map(tray => [tray.props['aria-label'], chips({ props: { children: tray.props.children } }).props.files.map(f => f.name)])
    assert.deepEqual(trays(), [['Files for this answer', []], ['Files for all answers', ['gone.txt']]])
    assert.equal(submit(card.result.current).props['aria-disabled'], true)
    editor(card.result.current, 'New question?').props.onChange('typed answer')
    assert.equal(submit(card.result.current).props.disabled, false)
    submit(card.result.current).props.onClick({ currentTarget: { closest: () => null } })
    await tick()
    assert.equal(posted[0], '- New question?: typed answer')
    assert.deepEqual(posted[3].attachments, [{ name: 'gone.txt', size: 1, mime_type: 'text/plain' }])
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
    if (url.endsWith('/upload-limits')) return Promise.resolve({ ok: true, json: async () => ({ max_bytes: 50 * 1024 * 1024 }) })
    if (options?.method === 'POST') uploads += 1
    return new Promise(() => {})
  }
  const card = renderHook(QuestionCard, {
    chatId: 'limit-chat', questionId: 'limit-card', questions: questions.slice(0, 1),
  })
  try {
    const many = Array.from({ length: 21 }, (_, i) => new File(['x'], `f${i}.txt`))
    editor(card.result.current, 'First?').props.onPasteFiles(many)
    await tick()
    assert.equal(chips(card.result.current).props.files.length, 20)
    assert.equal(uploads, 1, 'uploads run one at a time')
    const error = find(card.result.current, node => node.props?.className === 'qcard__submit-error')
    assert.equal(error.props.children, 'Attach at most 20 files to one card.')
  } finally { card.unmount(); globalThis.fetch = originalFetch }
})
