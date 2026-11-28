// An agent's key request as a saved chat card (KeyQuestionCard.jsx): the
// ordinary QuestionCard, whose Submit answers the vault first through the
// owner-only routes and only then records the card's answer. Submit wakes
// only once the card has been seen, still and untouched for a moment; a
// pasted key goes to the vault alone, never into the card's answer; a
// shared-browser guest never answers; and Allow on a key another app saved
// waits behind "Use <app>'s key anyway", as on every key card.
//
// The card's own hooks run under the hook harness (as in
// questionAttachmentInteractions.test.js); the vault, the key list and the
// owner's sign-in are stubbed where the key card reads them.
import assert from 'node:assert/strict'
import { after, beforeEach, test } from 'node:test'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const hooksPath = '/src/components/ChatView/hooks/__tests__/react-hook-shim.mjs'
// Where the key card reads the vault, the key list and the owner's sign-in.
const STUBS = {
  'key-card-client': `export const isSharedBrowserAuth = () => Boolean(globalThis.__keyCard.guest)
export const api = { keys: {
  approve: (...args) => globalThis.__keyCard.vaultCall('approve', ...args),
  deny: (...args) => globalThis.__keyCard.vaultCall('deny', ...args),
} }
export const jsonOrThrow = async response => response`,
  'key-card-queries': `export const KEYS_QUERY = ['settings', 'keys']
export const useKeys = () => ({ data: globalThis.__keyCard.vault, error: null })
export const useAppNames = () => ({ apps: [], names: { 8: 'Notes' } })`,
  'key-card-react-query': 'export const useQueryClient = () => ({ invalidateQueries() {} })',
}
const vite = await createSsrRenderServer({
  plugins: [{
    name: 'key-card-hooks', enforce: 'pre',
    transform(source, id) {
      if (!/\/(QuestionCard\.jsx|useFileUpload\.js|useKeyRequestCard\.js|KeyRequest\.jsx|useKeyAnswerGuard\.js)$/.test(id)) return undefined
      let out = source.replaceAll("from 'react'", "from 'question-card-hooks'")
      if (/\/(useKeyRequestCard\.js|KeyRequest\.jsx)$/.test(id)) {
        out = out.replace(/from '[./]+\/api\/client\.js'/, "from 'key-card-client'")
          .replace(/from '[./]+\/SettingsView\/keysQueries\.js'/, "from 'key-card-queries'")
          .replace("from '@tanstack/react-query'", "from 'key-card-react-query'")
      }
      return out
    },
    resolveId(id) {
      if (id === 'question-card-hooks' || STUBS[id]) return `\0${id}`
      return undefined
    },
    load(id) {
      if (id === '\0question-card-hooks') return `export * from '${hooksPath}'; export const useContext = () => null; export const useId = () => 'qcard';`
      return id.startsWith('\0') ? STUBS[id.slice(1)] : undefined
    },
  }],
})
const { renderHook } = await vite.ssrLoadModule(hooksPath)
const { default: KeyQuestionCard } = await vite.ssrLoadModule('/src/components/ChatView/KeyQuestionCard.jsx')
const { default: QuestionCard } = await vite.ssrLoadModule('/src/components/ChatView/QuestionCard.jsx')
const { default: KeyRequestDetails } = await vite.ssrLoadModule('/src/components/ChatView/KeyRequestDetails.jsx')
const { KeyAnyway, KeyPasteField } = await vite.ssrLoadModule('/src/components/SettingsView/KeyRequest.jsx')
after(() => vite.close())

// The page the card is on, and its clock: the answer guard reads both.
globalThis.document = Object.assign(new EventTarget(), { visibilityState: 'visible' })
globalThis.window = new EventTarget()
let now = 1_000
performance.now = () => now

function elements(node) {
  if (!node || typeof node !== 'object') return []
  if (Array.isArray(node)) return node.flatMap(elements)
  return [node, ...elements(node.props?.children)]
}
function find(tree, predicate) {
  const element = elements(tree).find(predicate)
  assert.ok(element, 'expected rendered element')
  return element
}
const optionLabels = tree => elements(tree).filter(node => node.props?.role === 'radio')
  .map(node => elements(node.props.children).find(child => typeof child.props?.text === 'string').props.text)
const option = (tree, label) => find(tree, node => node.props?.role === 'radio'
  && elements(node.props.children).some(child => child.props?.text === label))
const submit = tree => find(tree, node => node.props?.className?.split(' ').includes('qcard__submit'))
const tick = () => new Promise(resolve => setImmediate(resolve))

const SITE = { host: 'api.example.com', methods: ['GET'], paths: ['/*'] }
const QUESTION = 'Let your agent send **WEATHER_API** to **api.example.com**?'
const questions = [{
  id: 'key', header: 'Key', question: QUESTION, options: [
    { id: 'allow', label: 'Allow', description: 'Your agents may read WEATHER_API and send it to api.example.com.' },
    { id: 'deny', label: 'Not now', description: "Your agent carries on without it, and can't ask for this key again until you let it in Settings › Keys." },
  ],
}]
const keyRequest = { id: 'agent-WEATHER_API', key: 'WEATHER_API', reason: 'Show the forecast', sites: [SITE] }
const pending = (extra = {}) => ({
  id: 'agent-WEATHER_API', requester: 'agent', app: null, key: 'WEATHER_API', reason: 'Show the forecast',
  sites: [SITE], status: 'pending', ticket: 't-1', chat_id: 'chat-1', key_exists: true, key_origin: null,
  inject: null, sets_inject: false, inject_kept: false, ...extra,
})

let calls
let questionCount = 0
beforeEach(() => {
  calls = []
  globalThis.__keyCard = {
    guest: false,
    vault: { available: true, keys: [], requests: [pending()] },
    vaultCall: async (...args) => { calls.push(args); return { ok: true } },
  }
})

/** The card as the chat shows it: the key wrapper and the QuestionCard inside it. */
function renderCard() {
  questionCount += 1
  const props = {
    chatId: 'chat-1', questionId: `key-card-${questionCount}`, questions,
    keyRequest,
    onAnswer: (...args) => { calls.push(['answer', ...args]); return true },
  }
  return renderHook(() => {
    const outer = KeyQuestionCard(props)
    const inner = outer.props.children
    return { outer, card: QuestionCard(inner.props), keyCard: inner.props.keyCard }
  })
}
// A real tap on Submit: its press lands on the card first, then the click.
const submitNode = { closest: () => null, contains: target => target === submitNode, textContent: 'Submit' }
function tapSubmit(view, at) {
  view.result.current.outer.props.onPointerDownCapture({ timeStamp: at, target: submitNode })
  submit(view.result.current.card).props.onClick({ detail: 1, timeStamp: at + 60, currentTarget: submitNode })
}
const choose = (view, label) => option(view.result.current.card, label).props.onKeyDown({ key: 'Enter', preventDefault() {} })

test('Submit inside the wait answers nothing: no vault call and no card answer', async () => {
  now += 100_000
  const shown = now
  const view = renderCard()
  try {
    choose(view, 'Allow')
    assert.equal(submit(view.result.current.card).props['aria-disabled'], true)
    tapSubmit(view, shown + 200)
    await tick()
    assert.deepEqual(calls, [])
    assert.equal(view.result.current.keyCard.guard.early, 'Submit')
    // The wait is said in view, in the key card's own hint, not in the
    // visually hidden Submit hint every card carries.
    const hint = find(view.result.current.card, node => node.props?.className === 'qcard__key-hint')
    assert.equal(hint?.props.role, 'status')
  } finally { view.unmount() }
})

test('the vault hears Allow before the card records it, and a pasted key never reaches the answer', async () => {
  globalThis.__keyCard.vault.requests = [pending({ key_exists: false })]
  now += 100_000
  const shown = now
  const view = renderCard()
  try {
    choose(view, 'Allow')
    const field = find(view.result.current.card, node => node.type === KeyPasteField)
    field.props.answer.setValue('sk-made-up-secret')
    tapSubmit(view, shown + 2_000)
    for (let i = 0; i < 5; i += 1) await tick()
    assert.deepEqual(calls.map(([kind]) => kind), ['approve', 'answer'])
    const [, id, body] = calls[0]
    assert.equal(id, 'agent-WEATHER_API')
    assert.equal(body.value, 'sk-made-up-secret')
    assert.equal(body.ticket, 't-1')
    assert.doesNotMatch(JSON.stringify(calls[1]), /sk-made-up-secret/)
    assert.match(calls[1][1], /Allow/)
  } finally { view.unmount() }
})

test('a shared-browser guest never answers, even after the wait', async () => {
  globalThis.__keyCard.guest = true
  now += 100_000
  const shown = now
  const view = renderCard()
  try {
    choose(view, 'Allow')
    assert.equal(submit(view.result.current.card).props.disabled, true)
    tapSubmit(view, shown + 2_000)
    await tick()
    assert.deepEqual(calls, [])
  } finally { view.unmount() }
})

test("Allow on a key another app saved waits behind \"Use <app>'s key anyway\"", () => {
  globalThis.__keyCard.vault.requests = [pending({ key_origin: { app: 8, name: 'WEATHER_API' } })]
  now += 100_000
  const view = renderCard()
  try {
    assert.deepEqual(optionLabels(view.result.current.card), ['Not now'])
    const details = find(view.result.current.card, node => node.type === KeyRequestDetails)
    const anyway = find(KeyRequestDetails(details.props), node => node.type === KeyAnyway)
    assert.equal(anyway.props.anyway.label, "Use Notes's key anyway")
    anyway.props.onOpen()
    assert.deepEqual(optionLabels(view.result.current.card), ['Allow', 'Not now'])
  } finally { view.unmount() }
})

test("a request no longer waiting says what the vault shows, and takes only the true answer", async () => {
  // Allowed in Settings meanwhile: the agents may already send the key there.
  globalThis.__keyCard.vault = {
    available: true, requests: [], keys: [{ name: 'WEATHER_API', grants: { agents: true, sites: [SITE], apps: [] } }],
  }
  now += 100_000
  const shown = now
  const view = renderCard()
  try {
    const details = find(view.result.current.card, node => node.type === KeyRequestDetails)
    assert.match(elements(KeyRequestDetails(details.props))[0].props.children, /may already use this key.*Choose Allow/)
    choose(view, 'Not now')
    tapSubmit(view, shown + 2_000)
    for (let i = 0; i < 5; i += 1) await tick()
    assert.deepEqual(calls, [])
    choose(view, 'Allow')
    tapSubmit(view, shown + 4_000)
    for (let i = 0; i < 5; i += 1) await tick()
    // Nothing left to tell the vault: the card only records the answer.
    assert.deepEqual(calls.map(([kind]) => kind), ['answer'])
  } finally { view.unmount() }
})

const PARTS = [
  { name: 'account', label: 'Account SID', secret: false },
  { name: 'token', label: 'Auth token', secret: true },
  { name: 'pem', label: 'Key file', secret: true, multiline: true },
]

test('each part gets its own field: plain in the open, secret masked, a key file in a box that takes a drop', () => {
  const answer = {
    parts: PARTS, partValues: {}, setPartValue() {}, words: { pasteTitle: 'Paste the parts', pasteNote: 'Saved as x' },
  }
  const view = renderHook(() => KeyPasteField({ answer, disabled: false }))
  try {
    const tree = view.result.current
    const inputs = elements(tree).filter(node => node.type === 'input' || node.type === 'textarea')
    assert.deepEqual(inputs.map(node => [node.type, node.props.type ?? null]),
      [['input', 'text'], ['input', 'password'], ['textarea', null]])
    assert.equal(inputs[2].props['data-own-file-drop'], '', 'the key-file box takes a dropped file itself')
    assert.ok(elements(tree).some(node => /Account SID isn't secret/.test(node.props?.description || '')))
  } finally { view.unmount() }
})

test('Allow sends exactly this request\'s parts, and only once every one has a value', async () => {
  globalThis.__keyCard.vault.requests = [pending({
    key_exists: false, parts: PARTS,
    inject: { type: 'slots', slots: [{ type: 'header', name: 'X-Key', template: '{account}:{token}' }] },
  })]
  now += 100_000
  const shown = now
  const view = renderCard()
  try {
    choose(view, 'Allow')
    const answer = () => find(view.result.current.card, node => node.type === KeyPasteField).props.answer
    answer().setPartValue('stale', 'typed for an earlier request')
    answer().setPartValue('account', 'AC-made-up')
    answer().setPartValue('token', 'made-up-token-0123')
    assert.equal(view.result.current.keyCard.ready('allow'), false, 'every part needs a value')
    answer().setPartValue('pem', '-----BEGIN KEY-----')
    assert.equal(view.result.current.keyCard.ready('allow'), true)
    tapSubmit(view, shown + 2_000)
    for (let i = 0; i < 5; i += 1) await tick()
    const [kind, , body] = calls[0]
    assert.equal(kind, 'approve')
    assert.deepEqual(body.parts, { account: 'AC-made-up', token: 'made-up-token-0123', pem: '-----BEGIN KEY-----' })
    assert.equal(body.value, undefined)
  } finally { view.unmount() }
})
