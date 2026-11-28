import assert from 'node:assert/strict'
import { after, afterEach, beforeEach, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import StaticQuestionCard from '../QuestionCard.jsx'
import {
  applyRequestEntryState,
  applyRequestEntryStateToMessages,
  cardMarkersAfterLiveCard,
  entryVerdictFromDetail,
  reconcileUnknownPress,
  restartPressFailureOutcome,
  shownEntries,
} from '../requestEntries.js'
import { upsertQuestionItem } from '../streamReducers.js'

// One request-list card holds every kind of owner request. Questions and
// approvals share the card's Submit; a restart and a secret each act on their
// own. These tests drive the real components' handlers through the hook shim,
// with the secure-input API replaced by a recorder.
const hooksPath = '/src/components/ChatView/hooks/__tests__/react-hook-shim.mjs'
const vite = await createSsrRenderServer({
  plugins: [{
    name: 'mixed-card-hooks', enforce: 'pre',
    transform(source, id) {
      if (/\/(QuestionCard\.jsx|useFileUpload\.js|RequestEntries\.jsx|SecureInputCard\.jsx)$/.test(id)) {
        return source
          .replaceAll("from 'react'", "from 'mixed-card-hooks'")
          .replaceAll("from '../../api/client.js'", "from 'mixed-card-api'")
      }
    },
    resolveId(id) {
      if (id === 'mixed-card-hooks') return '\0mixed-card-hooks'
      if (id === 'mixed-card-api') return '\0mixed-card-api'
    },
    load(id) {
      if (id === '\0mixed-card-hooks') return `export * from '${hooksPath}'; export const useContext = () => null; export const useId = () => 'qcard';`
      if (id === '\0mixed-card-api') {
        return `
          const call = (name, args) => globalThis.__secureApi[name](...args)
          export const api = { secureInputs: {
            submit: (...args) => call('submit', args),
            cancel: (...args) => call('cancel', args),
            savedState: (...args) => call('savedState', args),
          } }
          export async function jsonOrThrow(response) {
            if (response.ok) return response.body
            const error = new Error('raw transport detail')
            error.status = response.status
            error.detail = response.detail
            throw error
          }`
      }
    },
  }],
})
const { renderHook } = await vite.ssrLoadModule(hooksPath)
const { default: QuestionCard } = await vite.ssrLoadModule('/src/components/ChatView/QuestionCard.jsx')
const { RestartEntry, SecretEntry } = await vite.ssrLoadModule('/src/components/ChatView/RequestEntries.jsx')
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
let apiCalls
beforeEach(() => {
  storageDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  apiCalls = []
  globalThis.__secureApi = {
    submit: async (...args) => {
      // Keep the very object the component sent, to prove it is cleared.
      apiCalls.push(['submit', args, structuredClone(args[2])])
      return { ok: true, body: { status: 'consuming' } }
    },
    cancel: async (...args) => { apiCalls.push(['cancel', args]); return { ok: true, body: { status: 'cancelled' } } },
    savedState: async (...args) => { apiCalls.push(['savedState', args]); return { ok: true, body: { status: 'pending' } } },
  }
})
afterEach(() => {
  if (storageDescriptor) Object.defineProperty(globalThis, 'localStorage', storageDescriptor)
  else delete globalThis.localStorage
  delete globalThis.__secureApi
})

function elements(node) {
  if (!node || typeof node !== 'object') return []
  if (Array.isArray(node)) return node.flatMap(elements)
  const own = [node]
  for (const value of Object.values(node.props || {})) {
    if (value && typeof value === 'object' && (value.$$typeof || Array.isArray(value))) {
      own.push(...elements(value))
    }
  }
  return own
}
const byClass = (tree, name) => elements(tree)
  .filter(node => node.props?.className?.split?.(' ').includes(name))
// The card's shared Submit; a restart entry's own Continue is not it.
const submit = tree => byClass(tree, 'qcard__submit')
  .find(node => !node.props.className.split(' ').includes('qcard__submit--entry'))
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
const entry = (tree, kind) => elements(tree).find(node => node.props?.entry?.kind === kind)
const option = (tree, label) => elements(tree).find(node => (
  node.props?.role && ['radio', 'checkbox'].includes(node.props.role)
  && elements(node.props.children).some(child => child.props?.text === label)
))
const click = node => node.props.onClick({ target: { closest: () => null }, currentTarget: { closest: () => null }, detail: 0 })
const tick = () => new Promise(resolve => setImmediate(resolve))
const text = tree => JSON.stringify(tree, (key, value) => (key.startsWith('_') ? undefined : value))

const secret = {
  id: 'req-secret', request_key: 'mail-key', request_revision: 1, kind: 'secret',
  header: 'Secure input', question: 'Connect the mail service', options: [],
  secure_input: {
    title: 'Connect the mail service', description: 'The key goes straight to the mail connector.',
    mode: 'sealed', fields: [{ name: 'api_key', label: 'API key', type: 'password', autocomplete: 'off' }],
  },
}
const tone = {
  id: 'req-tone', request_key: 'tone', request_revision: 1, kind: 'question',
  header: 'Question', question: 'How formal?', options: [],
}
const publish = {
  id: 'req-publish', request_key: 'publish', request_revision: 1, kind: 'approval',
  header: 'Approval', question: 'Publish the notes?', action_key: 'site:notes:publish',
  options: [
    { id: '0', label: 'Publish', description: 'Post them to the site now.' },
    { id: '1', label: 'Not now', description: 'Keep them as a draft.' },
  ],
}
const restart = {
  id: 'req-restart', request_key: 'restart', request_revision: 1, kind: 'restart',
  header: 'Restart Möbius', question: 'Restart Möbius now? This interrupts active turns for tens of seconds.',
  options: [{ id: 'opt-restart', label: 'Restart now', on_answer: 'close', description: 'Finish active work, then restart once.' }],
  platform_action: {
    version: 2, type: 'restart', action_id: 'platform-restart:x', restart_option_id: 'opt-restart',
    requirement: {}, status: 'awaiting_owner', wait_id: 'activation-card-1',
  },
}
const mixed = [secret, tone, publish, restart]
const props = extra => ({
  chatId: 'mixed-chat', questionId: 'card-1', inputRequests: true, questions: mixed, ...extra,
})

test('every answer box is named by its question without carrying it as data', () => {
  const card = renderHook(QuestionCard, props({ onAnswer: () => true, onRestartEntry: () => Promise.resolve({}) }))
  try {
    const boxes = elements(card.result.current)
      .filter(node => node.props && 'onChange' in node.props && 'onSubmitShortcut' in node.props)
    assert.equal(boxes.length, 3, 'the question, the approval and the restart reply')
    for (const box of boxes) {
      assert.ok(!('question' in box.props), 'an answer box reads no question prop')
      assert.ok(box.props.label || box.props.questionTextId, 'and is still named by its question')
    }
  } finally { card.unmount() }
})

test('the same secret on two cards in one transcript names each form by its own title', () => {
  const card = questionId => createElement(StaticQuestionCard, props({ questionId, questions: [secret], onAnswer: () => true }))
  const html = renderToStaticMarkup(createElement('div', null, card('card-1'), card('card-2')))
  const titles = [...html.matchAll(/<div class="qcard__text" id="([^"]+)">Connect the mail service</g)].map(match => match[1])
  const forms = [...html.matchAll(/class="secure-card__form qcard__secret-form"[^>]*aria-labelledby="([^"]+)"/g)].map(match => match[1])
  assert.equal(titles.length, 2)
  assert.notEqual(titles[0], titles[1])
  assert.deepEqual(forms, titles)
})

test('every kind renders as an entry of one card, in the given order', () => {
  const html = renderToStaticMarkup(createElement(StaticQuestionCard, props({
    onAnswer: () => true, onRestartEntry: () => Promise.resolve({}),
  })))
  const order = ['Connect the mail service', 'How formal?', 'Publish the notes?', 'Restart Möbius now?']
    .map(prompt => html.indexOf(prompt))
  assert.ok(order.every((at, i) => at >= 0 && (i === 0 || at > order[i - 1])), `entries in order: ${order}`)
  assert.equal((html.match(/class="qcard /g) || []).length + (html.match(/class="qcard"/g) || []).length, 1, 'one card')
  assert.match(html, /class="qcard__group-title">4 requests</)
  // Calm: no helper sentence under the title, no title above a question.
  assert.doesNotMatch(html, /qcard__group-copy|Anything you skip|>Question</)
  assert.match(html, /<div class="qcard__header">Approval<\/div>/)
  // The secret: sealed, uncontrolled, labelled fields with their own actions.
  const titleId = html.match(/<div class="qcard__text" id="([^"]+)">Connect the mail service</)?.[1]
  assert.ok(titleId, 'the secret names its form by its title')
  assert.ok(html.includes(`class="secure-card__form qcard__secret-form" autoComplete="off" aria-labelledby="${titleId}"`), html)
  assert.match(html, /<label class="secure-card__field"><span>API key<\/span><input type="text" autoComplete="off" required="" data-secure-field="api_key" data-secure-masked="true"/)
  assert.doesNotMatch(html, /value=/)
  assert.match(html, />Enter securely</)
  assert.match(html, /class="secure-card__cancel"[^>]*>Cancel</)
  // The approval: a labelled decision, with a plain reply box beside it.
  assert.match(html, /role="radiogroup" aria-label="Decision: Publish the notes\?"/)
  assert.match(html, />Choose one to decide</)
  assert.match(html, /aria-label="Reply instead of deciding: Publish the notes\?" placeholder="Or type a reply…"/)
  // The restart: the legacy Restart card's look, its choice and Continue.
  assert.match(html, /<div class="qcard__header">Restart Möbius<\/div>/)
  assert.match(html, /<div class="qcard__hint">Restart now, or reply below<\/div>/)
  assert.match(html, /role="radio" aria-checked="false" aria-describedby="[^"]+-opt0"><span class="qcard__mark qcard__mark--radio" aria-hidden="true"><\/span><span class="qcard__opt-label">Restart now<\/span>/)
  assert.match(html, /placeholder="Or tell me what you’d like to do instead…"/)
  assert.match(html, /<button type="button" class="qcard__submit qcard__submit--entry qcard__restart" disabled="">Continue<\/button>/)
  assert.doesNotMatch(html, /Reply instead<|reply-note|Sent to the agent as a message/)
  assert.equal((html.match(/<textarea/g) || []).length, 3, 'question, approval and restart each have a plain reply box')
  assert.doesNotMatch(html, /Still open/)
})

test('the shared Submit sends answered questions and selected approvals, never a secret or restart', async () => {
  let posted
  const card = renderHook(QuestionCard, props({ onAnswer: (...args) => { posted = args; return true } }))
  try {
    editor(card.result.current, 'How formal?').props.onChange('Casual')
    click(option(card.result.current, 'Publish'))
    assert.equal(submit(card.result.current).props.children, 'Submit 2 of 4 answers')
    assert.equal(hint(card.result.current).props.children, '', 'the count is in the button, not a line above it')
    click(submit(card.result.current))
    await tick()
    assert.deepEqual(posted[1], { 'How formal?': 'Casual', 'Publish the notes?': 'Publish' })
    assert.deepEqual(posted[3].selected_options, { 'req-publish': ['0'] })
    assert.equal(posted[3].closeOnlySelection, false)
    assert.equal(posted[0], '- How formal?: Casual\n- Publish the notes?: Publish')
    assert.doesNotMatch(JSON.stringify(posted), /req-secret|req-restart|opt-restart|Connect the mail/)
  } finally { card.unmount() }
})

test('a reply written on an approval is sent as a reply, not as a selection', async () => {
  let posted
  const card = renderHook(QuestionCard, props({
    questions: [publish], onAnswer: (...args) => { posted = args; return true },
  }))
  try {
    assert.equal(byClass(card.result.current, 'qcard__reply-toggle').length, 0, 'no Reply instead disclosure')
    click(option(card.result.current, 'Publish'))
    const reply = editor(card.result.current, 'Publish the notes?')
    assert.equal(reply.props.label, 'Reply instead of deciding: Publish the notes?')
    assert.equal(reply.props.placeholder, 'Or type a reply…')
    reply.props.onChange('Wait until Monday')
    // Writing replaces the choice: the approval is no longer selected.
    assert.equal(option(card.result.current, 'Publish').props['aria-checked'], false)
    // Choosing again clears the reply; writing again clears the choice.
    click(option(card.result.current, 'Not now'))
    assert.equal(editor(card.result.current, 'Publish the notes?').props.value, '')
    editor(card.result.current, 'Publish the notes?').props.onChange('Wait until Monday')
    assert.equal(option(card.result.current, 'Not now').props['aria-checked'], false)
    click(submit(card.result.current))
    await tick()
    assert.deepEqual(posted[1], { 'Publish the notes?': 'Wait until Monday' })
    assert.deepEqual(posted[3].selected_options, {})
  } finally { card.unmount() }
  // Shown afterwards as a reply that decided nothing.
  const html = renderToStaticMarkup(createElement(StaticQuestionCard, props({
    questions: [publish], answeredMap: { 'Publish the notes?': 'Wait until Monday' }, submittedOptions: {},
  })))
  assert.match(html, /Replied instead<span class="qcard__action-detail">Your reply went to the agent as a message\. This was neither approved nor declined\./)
  assert.match(html, /Wait until Monday/)
})

test('Restart now sends only its own selection and stops being pressable once requested', async () => {
  const presses = []
  const pressed = {
    status: 'restart_requested', text: 'Restart now', selected: ['opt-restart'],
    platform_action: { ...restart.platform_action, status: 'restart_requested' },
  }
  const card = renderHook(QuestionCard, props({
    onAnswer: () => true,
    onRestartEntry: press => { presses.push(press); return Promise.resolve({ status: 'restart_requested', state: pressed }) },
  }))
  try {
    const restartEntry = entry(card.result.current, 'restart')
    assert.equal(restartEntry.props.interactive, true)
    await restartEntry.props.onPress()
    assert.deepEqual(presses, [{
      questionId: 'card-1', requestId: 'req-restart', selectedOptions: { 'req-restart': ['opt-restart'] },
    }])
    restartEntry.props.onState('req-restart', pressed)
    assert.deepEqual(entry(card.result.current, 'restart').props.state, pressed)
    // A pressed restart leaves the shared Submit to the other entries.
    editor(card.result.current, 'How formal?').props.onChange('Casual')
    assert.equal(submit(card.result.current).props.children, 'Submit 1 of 3 answers')
  } finally { card.unmount() }

  let calls = 0
  let release
  const live = renderHook(RestartEntry, {
    entry: restart, state: null, interactive: true, chosen: true,
    onPress: () => { calls++; return new Promise(resolve => { release = resolve }) },
    onState: () => {},
  })
  try {
    click(byClass(live.result.current, 'qcard__restart')[0])
    click(byClass(live.result.current, 'qcard__restart')[0])
    assert.equal(calls, 1, 'a second press while the first is in flight sends nothing')
    assert.equal(byClass(live.result.current, 'qcard__restart')[0].props.disabled, true)
    release({ state: pressed })
    await tick()
  } finally { live.unmount() }

  const settled = renderToStaticMarkup(createElement(RestartEntry, {
    entry: restart, state: pressed, interactive: true, chosen: true,
  }))
  assert.doesNotMatch(settled, /qcard__restart"|role="radio"|<textarea|Reply instead/)
  // The legacy Restart card's status line, in the slot the controls had:
  // the heading and prompt above it never move.
  assert.match(settled, /^<div class="qcard__header">Restart Möbius<\/div><div class="qcard__text"[^>]*>.*?<\/div><div class="qcard__action-status qcard__entry-settled" role="status">Restart requested<span class="qcard__action-detail">Möbius is draining active work and will restart once\.<\/span><\/div>$/)
  const activated = renderToStaticMarkup(createElement(RestartEntry, {
    entry: restart, state: { ...pressed, status: 'activated' }, interactive: true,
  }))
  assert.match(activated, />Möbius restarted</)
})

const SENTINEL = 'SENTINEL-not-a-secret-999'
const refusal = (status, extra = {}) => Object.assign(new Error(`HTTP ${status} ${SENTINEL}`), { status, detail: `${SENTINEL} detail`, ...extra })
const restartProps = extra => ({ entry: restart, state: null, interactive: true, chosen: true, onState: () => {}, ...extra })

test('a refused or failed Restart now says what the server recorded, never server text', async () => {
  // Every failure reaches the entry already reconciled (ChatView's press reads
  // the server's record first, restartPressFailureOutcome).
  const open = (failure, extra = {}) => Object.assign(failure, { reconciled: 'open', ...extra })
  const cases = [
    [open(refusal(409)), /didn’t accept that press, and the restart is still offered\. Press Continue again/, false],
    [open(refusal(400)), /didn’t accept that press, and the restart is still offered/, false],
    [open(refusal(409, { code: 'restart_action_conflict' })), /no longer active, so that press didn’t count/, false],
    [open(new TypeError('Failed to fetch')), /That press didn’t reach Möbius\. The restart is still offered/, false],
    [open(new TypeError('Failed to fetch'), { outcomeUnknown: true }), /didn’t confirm that press, and the restart is still offered, so it wasn’t recorded/, false],
    [Object.assign(refusal(410, { code: 'question_state_changed' }), { reconciled: 'gone' }), /no longer offered on this card/, true],
    [Object.assign(refusal(409), { reconciled: 'unknown' }), /couldn’t confirm what happened to that press/, true],
    [Object.assign(new TypeError('Failed to fetch'), { outcomeUnknown: true, reconciled: 'unknown' }), /may already be under way/, true],
  ]
  for (const [failure, copy, inert] of cases) {
    const card = renderHook(RestartEntry, restartProps({ onPress: () => Promise.reject(failure) }))
    try {
      click(byClass(card.result.current, 'qcard__restart')[0])
      await tick()
      const shown = byClass(card.result.current, 'qcard__submit-error')[0].props.children
      assert.match(shown, copy)
      assert.doesNotMatch(shown, /Nothing restarted/, 'never claimed without the server showing it')
      assert.doesNotMatch(text(card.result.current), new RegExp(SENTINEL))
      assert.equal(byClass(card.result.current, 'qcard__restart')[0].props.disabled, inert,
        inert ? 'pressed again only once the server shows it still offered' : 'still offered: may be pressed again')
    } finally { card.unmount() }
  }
  const html = renderToStaticMarkup(createElement(StaticQuestionCard, props({ onAnswer: () => true })))
  assert.doesNotMatch(html, new RegExp(SENTINEL))
})

test('a press refused before activation is reconciled from the server record', async () => {
  // A local 409 is indistinguishable from a server 4xx, so every failure is
  // reconciled before anything is shown.
  const { answerBeforeActivationError } = await import('../chatRuntimeState.js')
  const local = answerBeforeActivationError()
  assert.equal(local.status, 409)
  assert.equal(local.code, undefined, 'indistinguishable from a server 4xx by status')
  const landed = { status: 'restart_requested' }
  assert.deepEqual(await restartPressFailureOutcome(local, async () => ({ kind: 'settled', state: landed })),
    { state: landed }, 'a press that did land shows as requested, with no error')
  assert.equal((await restartPressFailureOutcome(local, async () => ({ kind: 'open' }))).error.reconciled, 'open')
  assert.equal((await restartPressFailureOutcome(new TypeError('x'), async () => ({ kind: 'unknown' }))).error.reconciled, 'unknown')
  assert.equal((await restartPressFailureOutcome(new TypeError('x'), async () => ({ kind: 'settled' }))).error.reconciled,
    'unknown', 'a verdict without a state is not a recorded press')
})

test('an unconfirmed press says the outcome is unknown and is offered again only once the entry is known to be open', async () => {
  const unknown = verdict => Object.assign(new TypeError('Failed to fetch'), { outcomeUnknown: true, reconciled: verdict })
  const stillOpen = renderHook(RestartEntry, restartProps({ onPress: () => Promise.reject(unknown('open')) }))
  try {
    click(byClass(stillOpen.result.current, 'qcard__restart')[0])
    await tick()
    assert.match(byClass(stillOpen.result.current, 'qcard__submit-error')[0].props.children,
      /didn’t confirm that press, and the restart is still offered, so it wasn’t recorded/)
    assert.equal(byClass(stillOpen.result.current, 'qcard__restart')[0].props.disabled, false)
  } finally { stillOpen.unmount() }

  const cannotTell = renderHook(RestartEntry, restartProps({ onPress: () => Promise.reject(unknown('unknown')) }))
  try {
    click(byClass(cannotTell.result.current, 'qcard__restart')[0])
    await tick()
    const shown = byClass(cannotTell.result.current, 'qcard__submit-error')[0].props.children
    assert.match(shown, /may already be under way/)
    assert.doesNotMatch(shown, /Nothing restarted/)
    assert.equal(byClass(cannotTell.result.current, 'qcard__restart')[0].props.disabled, true, 'never invited to press twice blind')
  } finally { cannotTell.unmount() }

  // The press landed though its response was lost: the server's state is shown.
  const landed = { status: 'restart_requested', selected: ['opt-restart'], platform_action: { ...restart.platform_action, status: 'restart_requested' } }
  const states = []
  const reconciled = renderHook(RestartEntry, restartProps({
    onPress: () => Promise.resolve({ status: 'restart_requested', state: landed, reconciled: true }),
    onState: (...args) => states.push(args),
  }))
  try {
    click(byClass(reconciled.result.current, 'qcard__restart')[0])
    await tick()
    assert.deepEqual(states, [['req-restart', landed]])
    assert.equal(byClass(reconciled.result.current, 'qcard__submit-error').length, 0)
  } finally { reconciled.unmount() }
})

test('a lost Restart now response is reconciled from what the server recorded', async () => {
  const card = listCardBlock => ({ open_request_card_id: 'card-1', messages: [{ role: 'assistant', blocks: [listCardBlock] }] })
  const block = { type: 'question', question_id: 'card-1', input_requests: true, questions: mixed }
  const landed = { status: 'restart_requested', selected: ['opt-restart'] }
  const read = detail => reconcileUnknownPress({ questionId: 'card-1', requestId: 'req-restart', readDetail: async () => detail })
  assert.deepEqual(await read(card({ ...block, request_states: { 'req-restart': landed } })), { kind: 'settled', state: landed })
  assert.deepEqual(await read(card(block)), { kind: 'open' })
  assert.deepEqual(await read({ ...card(block), open_request_card_id: 'card-2' }), { kind: 'gone' })
  assert.deepEqual(await read({ open_request_card_id: 'card-1', messages: [] }), { kind: 'unknown' })
  assert.deepEqual(await reconcileUnknownPress({
    questionId: 'card-1', requestId: 'req-restart', readDetail: async () => { throw new Error('offline') },
  }), { kind: 'unknown' })
  // A card answered in part stays open for the restart its answer left.
  assert.equal(entryVerdictFromDetail(card({ ...block, answers: { 'How formal?': 'Casual' } }), 'card-1', 'req-restart').kind, 'open')
  assert.equal(entryVerdictFromDetail(card({ ...block, answers: { [restart.question]: 'Not yet' } }), 'card-1', 'req-restart').kind, 'unknown')
})

test('two presses in the same tick send one request', async () => {
  let calls = 0
  let release
  const card = renderHook(RestartEntry, restartProps({
    onPress: () => { calls++; return new Promise(resolve => { release = resolve }) },
  }))
  try {
    const button = byClass(card.result.current, 'qcard__restart')[0]
    button.props.onClick()
    button.props.onClick()
    assert.equal(calls, 1)
    release({})
    await tick()
    byClass(card.result.current, 'qcard__restart')[0].props.onClick()
    assert.equal(calls, 2, 'released once the first press settled')
    release({})
    await tick()
  } finally { card.unmount() }
})

function fakeForm(value) {
  const input = { dataset: { secureField: 'api_key' }, value, blur() {} }
  return {
    input,
    form: {
      reportValidity: () => true,
      querySelectorAll: () => [input],
      reset() { input.value = '' },
      elements: {},
    },
  }
}

test('a secret is submitted on its own endpoint and leaves no value behind', async () => {
  const states = []
  const card = renderHook(SecretEntry, {
    chatId: 'mixed-chat', cardId: 'card-1', entry: secret, state: null, interactive: true,
    onState: (...args) => states.push(args),
  })
  try {
    await tick()
    assert.deepEqual(apiCalls.map(([name]) => name), ['savedState'])
    const formNode = elements(card.result.current).find(node => node.type === 'form')
    const { form, input } = fakeForm('typed-value-1')
    formNode.ref.current = form
    await formNode.props.onSubmit({ preventDefault() {} })
    await tick()
    const [, args, sent] = apiCalls.find(([name]) => name === 'submit')
    assert.deepEqual(args.slice(0, 2), ['mixed-chat', 'req-secret'])
    assert.deepEqual(sent, {
      question_id: 'card-1', request_revision: 1, fields: { api_key: 'typed-value-1' },
    }, 'the documented body, naming the card and revision it shows, nothing else')
    assert.equal(args[2].fields.api_key, '', 'the sent values are dropped once settled')
    assert.equal(input.value, '', 'the field is cleared before the request leaves')
    assert.deepEqual(states, [['req-secret', { status: 'consuming' }]])
    assert.doesNotMatch(text(card.result.current), /typed-value-1/)
  } finally { card.unmount() }
  assert.equal(globalThis.localStorage.length, 0)
})

test('a secret never enters the request drafts, and a settled entry leaves the shared Submit', () => {
  const card = renderHook(QuestionCard, props({ onAnswer: () => true }))
  try {
    editor(card.result.current, 'How formal?').props.onChange('Casual')
    entry(card.result.current, 'secret').props.onState('req-secret', { status: 'consuming' })
    assert.deepEqual(entry(card.result.current, 'secret').props.state, { status: 'consuming' })
    assert.equal(submit(card.result.current).props.children, 'Submit 1 of 3 answers')
  } finally { card.unmount() }
  const keys = [...globalThis.localStorage.values.keys()]
  assert.ok(keys.some(key => key.endsWith(':request:req-tone')), 'the question keeps its draft')
  assert.ok(!keys.some(key => key.includes('req-secret')), 'a secret has no draft')
})

test('request_states and input_request_settled update the entry they name', () => {
  const block = { type: 'question', question_id: 'card-1', input_requests: true, questions: mixed }
  const event = { question_id: 'card-1', request_id: 'req-secret', kind: 'secret', state: { status: 'consuming' } }
  const [patched] = applyRequestEntryState([block], event)
  assert.deepEqual(patched.request_states, { 'req-secret': { status: 'consuming' } })
  const done = { status: 'completed', outcome: 'Secure input was consumed without exposing its values.' }
  const [completed] = applyRequestEntryState([patched], { ...event, state: done })
  assert.deepEqual(completed.request_states, { 'req-secret': done })
  // Another card, an unknown entry, or an ordinary card is left untouched.
  const other = [{ ...block, question_id: 'card-2' }]
  assert.equal(applyRequestEntryState(other, event), other)
  const legacy = [{ ...block, input_requests: undefined }]
  assert.equal(applyRequestEntryState(legacy, event), legacy)
  const messages = [{ role: 'user', content: 'hi' }, { role: 'assistant', blocks: [block] }]
  assert.deepEqual(applyRequestEntryStateToMessages(messages, event)[1].blocks[0].request_states,
    { 'req-secret': { status: 'consuming' } })
  assert.equal(applyRequestEntryStateToMessages(messages, { ...event, question_id: 'nope' }), messages)
  // A replayed card event keeps a state the stream has since seen.
  const replayed = upsertQuestionItem([completed], { ...block, request_states: { 'req-secret': { status: 'consuming' } } })
  assert.deepEqual(replayed[0].request_states, { 'req-secret': done })

  const html = renderToStaticMarkup(createElement(StaticQuestionCard, props({
    onAnswer: () => true, requestStates: completed.request_states,
  })))
  assert.match(html, /<strong>Provided securely<\/strong>/)
  assert.match(html, /class="qcard__entry-outcome" role="status">Secure input was consumed without exposing its values\.</)
  assert.doesNotMatch(html, /data-secure-field/)
  for (const [status, label] of [['consuming', 'Using securely…'], ['failed', 'Used securely'], ['cancelled', 'Not provided'], ['interrupted', 'Outcome unknown']]) {
    const settled = renderToStaticMarkup(createElement(StaticQuestionCard, props({
      onAnswer: () => true, requestStates: { 'req-secret': { status, ...(status === 'failed' ? { outcome: 'The sealed consumer failed; submitted values were discarded.' } : {}) } },
    })))
    assert.match(settled, new RegExp(`<strong>${label}</strong>`), status)
    if (status === 'failed') assert.match(settled, /The sealed consumer failed; submitted values were discarded\./)
  }
})

test('one Restart now press commits its state once, and the settled event after it commits nothing', async () => {
  const pressed = { status: 'restart_requested', selected: ['opt-restart'], platform_action: { ...restart.platform_action, status: 'restart_requested' } }
  let messages = [{ role: 'assistant', blocks: [{ type: 'question', question_id: 'card-1', input_requests: true, questions: mixed }] }]
  let commits = 0
  // What ChatView's commitRequestEntryState does with the transcript.
  const commit = event => {
    const next = applyRequestEntryStateToMessages(messages, event)
    if (next !== messages) { commits++; messages = next }
  }
  const live = renderHook(RestartEntry, restartProps({
    onPress: () => Promise.resolve({ status: 'restart_requested', state: pressed }),
    onState: (requestId, state) => commit({ question_id: 'card-1', request_id: requestId, state }),
  }))
  try {
    click(byClass(live.result.current, 'qcard__restart')[0])
    await tick()
  } finally { live.unmount() }
  assert.equal(commits, 1)
  commit({ type: 'input_request_settled', question_id: 'card-1', request_id: 'req-restart', kind: 'restart', state: structuredClone(pressed) })
  assert.equal(commits, 1, 'the identical settled event leaves the transcript as it is')
  commit({ question_id: 'card-1', request_id: 'req-restart', state: { ...pressed, status: 'activated' } })
  assert.equal(commits, 2)
})

test('a submitted card keeps only the entries answered or settled on it', () => {
  const pressed = { status: 'restart_requested', selected: ['opt-restart'], platform_action: { ...restart.platform_action, status: 'restart_requested' } }
  const card = renderHook(QuestionCard, props({
    answeredMap: { 'How formal?': 'Casual' }, submittedOptions: {},
    requestStates: { 'req-restart': pressed },
  }))
  try {
    const shown = byClass(card.result.current, 'qcard__q').map(node => node.props['data-question-index'])
    assert.deepEqual(shown, [1, 3], 'the unanswered secret and approval leave this card')
    assert.equal(entry(card.result.current, 'secret'), undefined)
    assert.equal(option(card.result.current, 'Publish'), undefined)
    assert.equal(submit(card.result.current).props.children, 'Submitted')
  } finally { card.unmount() }
  assert.deepEqual(shownEntries(mixed, { answers: {}, states: {}, closed: false }), mixed)
})

test('a card answered in part that is still open keeps its secret and restart usable', async () => {
  const pressed = []
  const card = renderHook(QuestionCard, props({
    answeredMap: { 'How formal?': 'Casual' }, submittedOptions: {}, entriesOffered: true,
    onRestartEntry: async press => { pressed.push(press); return {} },
  }))
  try {
    const shown = byClass(card.result.current, 'qcard__q').map(node => node.props['data-question-index'])
    assert.deepEqual(shown, [0, 1, 3], 'the answer, the secret and the restart; the unanswered approval waits')
    assert.equal(option(card.result.current, 'Publish'), undefined)
    assert.equal(entry(card.result.current, 'secret').props.interactive, true)
    const restartEntry = entry(card.result.current, 'restart')
    assert.equal(restartEntry.props.interactive, true)
    assert.equal(restartEntry.props.reply, null, 'the answers were sent: no reply box is left to send')
    assert.equal(restartEntry.props.onSubmitReply, undefined)
    restartEntry.props.onChoose()
    assert.equal(entry(card.result.current, 'restart').props.chosen, true, 'Restart now can still be chosen')
    await entry(card.result.current, 'restart').props.onPress()
    assert.deepEqual(pressed, [{ questionId: 'card-1', requestId: 'req-restart', selectedOptions: { 'req-restart': ['opt-restart'] } }])
    assert.equal(submit(card.result.current).props.children, 'Submitted')
  } finally { card.unmount() }

  // Once the next card replaces it, only what was answered here stays.
  const replaced = renderHook(QuestionCard, props({
    answeredMap: { 'How formal?': 'Casual' }, submittedOptions: {}, entriesOffered: false,
  }))
  try {
    assert.deepEqual(byClass(replaced.result.current, 'qcard__q').map(node => node.props['data-question-index']), [1])
  } finally { replaced.unmount() }
  const answers = { 'How formal?': 'Casual' }
  assert.deepEqual(shownEntries(mixed, { answers, closed: true, offered: true }).map(q => q.id), ['req-secret', 'req-tone', 'req-restart'])
  assert.deepEqual(shownEntries(mixed, { answers, closed: true, offered: true,
    states: { 'req-secret': { status: 'cancelled' } } }).map(q => q.id), ['req-secret', 'req-tone', 'req-restart'])
  assert.deepEqual(shownEntries(mixed, { answers, closed: true }).map(q => q.id), ['req-tone'])
})

test('a restart entry with no reply box offers only Restart now', () => {
  const html = renderToStaticMarkup(createElement(StaticQuestionCard, props({
    questions: [tone, restart], answeredMap: { 'How formal?': 'Casual' }, submittedOptions: {}, entriesOffered: true,
    onRestartEntry: () => Promise.resolve({}),
  })))
  assert.match(html, /class="qcard__hint">Restart now</)
  assert.doesNotMatch(html, /or reply below|Reply instead of restarting/)
})

test('a superseded card shows only what settled on it, without a Submit', () => {
  const card = renderHook(QuestionCard, props({
    disabled: true, requestStates: { 'req-secret': { status: 'completed' } },
  }))
  try {
    assert.deepEqual(byClass(card.result.current, 'qcard__q').map(node => node.props['data-question-index']), [0])
    assert.equal(entry(card.result.current, 'secret').props.interactive, false)
    assert.equal(submit(card.result.current), undefined)
  } finally { card.unmount() }
})

test('a 410 on submit explains itself by its code and keeps what the owner entered', async () => {
  const changed = 'A question on this card changed while you were answering. The updated card appears when the agent finishes this step.'
  const replaced = 'This card was replaced by a newer one. What you entered here is kept there.'
  // The copy follows the refusal's code, never the wording the server sent.
  let refusal = Object.assign(new Error('HTTP 410'), { status: 410, code: 'card_out_of_date', detail: `${SENTINEL} detail` })
  const card = renderHook(QuestionCard, props({ onAnswer: () => Promise.reject(refusal) }))
  try {
    editor(card.result.current, 'How formal?').props.onChange('Casual')
    click(option(card.result.current, 'Publish'))
    click(submit(card.result.current))
    await tick()
    assert.equal(byClass(card.result.current, 'qcard__submit-error')[0].props.children, changed)
    assert.equal(editor(card.result.current, 'How formal?').props.value, 'Casual')
    assert.equal(option(card.result.current, 'Publish').props['aria-checked'], true)
    assert.equal(entry(card.result.current, 'secret').props.interactive, true)

    for (const other of [
      { status: 410, code: 'question_state_changed', detail: 'The question is no longer accepting answers.' },
      { status: 410, detail: changed },
    ]) {
      refusal = Object.assign(new Error('HTTP 410'), other)
      click(submit(card.result.current))
      await tick()
      assert.equal(byClass(card.result.current, 'qcard__submit-error')[0].props.children, replaced)
    }
  } finally { card.unmount() }
})

test('a written reply to a restart joins the shared Submit without restarting', async () => {
  let posted
  const card = renderHook(QuestionCard, props({
    questions: [restart], onAnswer: (...args) => { posted = args; return true },
  }))
  try {
    // A restart alone has no shared Submit: its Continue sends the reply too.
    assert.equal(submit(card.result.current), undefined)
    assert.equal(entry(card.result.current, 'restart').props.replyReady, false, 'nothing to send until a reply is written')
    const reply = editor(entry(card.result.current, 'restart').props.reply, restart.question)
    assert.equal(reply.props.placeholder, 'Or tell me what you’d like to do instead…')
    reply.props.onChange('Not yet, a build is running')
    assert.equal(entry(card.result.current, 'restart').props.chosen, false)
    assert.equal(entry(card.result.current, 'restart').props.replyReady, true)
    entry(card.result.current, 'restart').props.onSubmitReply(null)
    await tick()
    assert.deepEqual(posted[1], { [restart.question]: 'Not yet, a build is running' })
    assert.deepEqual(posted[3].selected_options, {})
  } finally { card.unmount() }
})

test('a refused reply sent with a lone restart entry\u2019s Continue shows under that Continue', async () => {
  const card = renderHook(QuestionCard, props({
    questions: [restart], onAnswer: () => Promise.reject(refusal(409)),
  }))
  try {
    editor(entry(card.result.current, 'restart').props.reply, restart.question).props.onChange('Not yet')
    entry(card.result.current, 'restart').props.onSubmitReply(null)
    await tick()
    const shown = entry(card.result.current, 'restart').props.replyError
    assert.ok(shown, 'the card tells the owner the reply did not land')
    assert.doesNotMatch(shown, new RegExp(SENTINEL))
  } finally { card.unmount() }

  const shownEntry = renderHook(RestartEntry, restartProps({ chosen: false, replyError: 'That reply didn\u2019t save.' }))
  try {
    const all = elements(shownEntry.result.current)
    const error = byClass(shownEntry.result.current, 'qcard__submit-error')[0]
    assert.equal(error.props.children, 'That reply didn\u2019t save.')
    assert.ok(all.indexOf(error) > all.indexOf(byClass(shownEntry.result.current, 'qcard__restart')[0]),
      'under the Continue just pressed')
  } finally { shownEntry.unmount() }
})

function filledSecret(extra = {}) {
  const card = renderHook(SecretEntry, {
    chatId: 'mixed-chat', entry: secret, state: null, interactive: true, onState: () => {}, ...extra,
  })
  const formNode = elements(card.result.current).find(node => node.type === 'form')
  const { form, input } = fakeForm('typed-value-2')
  formNode.ref.current = form
  return { card, input, formNode }
}

test('a secret entry cancels only the card and revision it shows', async () => {
  const card = renderHook(QuestionCard, props({ onAnswer: () => true }))
  try {
    assert.equal(entry(card.result.current, 'secret').props.cardId, 'card-1')
  } finally { card.unmount() }
  const shown = filledSecret({ cardId: 'card-1', entry: { ...secret, request_revision: 3 } })
  try {
    await byClass(shown.card.result.current, 'secure-card__cancel')[0].props.onClick()
    await tick()
    assert.deepEqual(apiCalls.filter(([name]) => name === 'cancel').map(([, args]) => args),
      [['mixed-chat', 'req-secret', { question_id: 'card-1', request_revision: 3 }]])
  } finally { shown.card.unmount() }
})

test('filled sealed fields are wiped on cancel, when the entry stops being open, and on unmount', async () => {
  const cancelled = filledSecret()
  try {
    await byClass(cancelled.card.result.current, 'secure-card__cancel')[0].props.onClick()
    await tick()
    assert.equal(cancelled.input.value, '', 'cancel')
  } finally { cancelled.card.unmount() }

  const superseded = filledSecret()
  try {
    superseded.card.rerender({ chatId: 'mixed-chat', entry: secret, state: null, interactive: false, onState: () => {} })
    assert.equal(superseded.input.value, '', 'no longer open')
  } finally { superseded.card.unmount() }

  const settled = filledSecret()
  try {
    settled.card.rerender({ chatId: 'mixed-chat', entry: secret, state: { status: 'consuming' }, interactive: true, onState: () => {} })
    assert.equal(settled.input.value, '', 'settled elsewhere')
  } finally { settled.card.unmount() }

  const unmounted = filledSecret()
  unmounted.card.unmount()
  assert.equal(unmounted.input.value, '', 'unmount')
})

test('a sealed entry never shows server text and reads its receipt when no verdict came back', async () => {
  for (const [status, copy] of [[400, /weren’t accepted/], [409, /no longer open/], [410, /no longer offered on this card/], [503, /didn’t go through/]]) {
    globalThis.__secureApi.submit = async () => ({ ok: false, status, detail: `${SENTINEL} echoed` })
    const { card, formNode } = filledSecret()
    try {
      await formNode.props.onSubmit({ preventDefault() {} })
      await tick()
      assert.match(elements(card.result.current).find(node => node.props?.role === 'alert').props.children, copy)
      assert.doesNotMatch(text(card.result.current), new RegExp(SENTINEL))
    } finally { card.unmount() }
  }
  // A dropped response may have landed: the receipt decides, nothing resends.
  let submits = 0
  globalThis.__secureApi.submit = async () => { submits++; throw new TypeError('Failed to fetch') }
  globalThis.__secureApi.savedState = async () => ({ ok: true, body: { status: 'consuming' } })
  const states = []
  const { card, formNode } = filledSecret({ onState: (...args) => states.push(args) })
  try {
    await tick()
    states.length = 0
    await formNode.props.onSubmit({ preventDefault() {} })
    await tick()
    assert.equal(submits, 1)
    assert.deepEqual(states, [['req-secret', { status: 'consuming' }]])
  } finally { card.unmount() }
})

test('a list card shows fixed copy for a refused submission, never server text', async () => {
  for (const status of [409, 410]) {
    const card = renderHook(QuestionCard, props({ onAnswer: () => Promise.reject(refusal(status)) }))
    try {
      editor(card.result.current, 'How formal?').props.onChange('Casual')
      click(submit(card.result.current))
      await tick()
      const shown = byClass(card.result.current, 'qcard__submit-error')[0].props.children
      assert.ok(shown && !shown.includes(SENTINEL), shown)
      assert.doesNotMatch(text(card.result.current), new RegExp(SENTINEL))
    } finally { card.unmount() }
  }
})

test('choosing Restart now is never an answer: only Continue sends it, and no draft keeps it', async () => {
  let posted
  const card = renderHook(QuestionCard, props({ onAnswer: (...args) => { posted = args; return true } }))
  try {
    const restartEntry = () => entry(card.result.current, 'restart')
    restartEntry().props.onChoose()
    assert.equal(restartEntry().props.chosen, true)
    assert.equal(submit(card.result.current).props['aria-disabled'], true, 'the shared Submit has nothing to send')
    assert.equal(submit(card.result.current).props.children, 'Submit')
    // Writing a reply clears the choice; choosing again clears the reply.
    const reply = () => editor(restartEntry().props.reply, restart.question)
    reply().props.onChange('Not yet')
    assert.equal(restartEntry().props.chosen, false)
    restartEntry().props.onChoose()
    assert.equal(reply().props.value, '')
    assert.equal(restartEntry().props.chosen, true)
    editor(card.result.current, 'How formal?').props.onChange('Casual')
    click(submit(card.result.current))
    await tick()
    assert.deepEqual(posted[1], { 'How formal?': 'Casual' })
    assert.doesNotMatch(JSON.stringify(posted), /req-restart|opt-restart|Restart now/)
  } finally { card.unmount() }
  const drafts = [...globalThis.localStorage.values.values()].join('\n')
  assert.doesNotMatch(drafts, /Restart now/, 'a later card never comes back with the restart chosen')
})

test('a restart entry’s Continue presses only once chosen, and sends a reply only where it owns one', async () => {
  const presses = []
  const idle = renderHook(RestartEntry, restartProps({ chosen: false, onPress: () => { presses.push(1); return Promise.resolve({}) } }))
  try {
    const button = byClass(idle.result.current, 'qcard__restart')[0]
    assert.equal(button.props.children, 'Continue')
    assert.equal(button.props.disabled, true, 'nothing chosen, nothing to send')
    button.props.onClick({ currentTarget: { closest: () => null } })
    assert.deepEqual(presses, [])
  } finally { idle.unmount() }
  const replies = []
  const lone = renderHook(RestartEntry, restartProps({
    chosen: false, replyReady: true, onPress: () => { presses.push(1); return Promise.resolve({}) },
    onSubmitReply: card => replies.push(card),
  }))
  try {
    const button = byClass(lone.result.current, 'qcard__restart')[0]
    assert.equal(button.props.disabled, false)
    button.props.onClick({ currentTarget: { closest: () => 'the-card' } })
    assert.deepEqual(replies, ['the-card'])
    assert.deepEqual(presses, [], 'a written reply never restarts')
  } finally { lone.unmount() }
  const html = renderToStaticMarkup(createElement(RestartEntry, restartProps({ chosen: true, onPress: () => {} })))
  assert.match(html, /class="qcard__opt qcard__opt--on"/)
  assert.match(html, /<button type="button" class="qcard__submit qcard__submit--entry qcard__restart">Continue<\/button>/)
})

test('a partial Submit counts what it sends against every entry still open on the card', () => {
  const card = renderHook(QuestionCard, props({ questions: [tone, secret, restart], onAnswer: () => true }))
  try {
    assert.equal(submit(card.result.current).props.children, 'Submit')
    editor(card.result.current, 'How formal?').props.onChange('Casual')
    assert.equal(submit(card.result.current).props.children, 'Submit 1 of 3 answers')
    // A reply written on the restart is an answer too.
    editor(entry(card.result.current, 'restart').props.reply, restart.question).props.onChange('Not yet')
    assert.equal(submit(card.result.current).props.children, 'Submit 2 of 3 answers')
    // A settled secret leaves the count; everything open is answered.
    entry(card.result.current, 'secret').props.onState('req-secret', { status: 'consuming' })
    assert.equal(submit(card.result.current).props.children, 'Submit')
  } finally { card.unmount() }
})

test('only an approval, a secret and a restart keep a kind label; a question shows no title', () => {
  const html = renderToStaticMarkup(createElement(StaticQuestionCard, props({
    questions: [{ ...tone, header: 'Protected folder' }, { ...publish, header: 'Agent title' }, { ...secret, header: 'Agent title' }, restart],
    onAnswer: () => true,
  })))
  assert.doesNotMatch(html, /Protected folder|Agent title/)
  assert.match(html, /<div class="qcard__header">Approval<\/div>/)
  assert.match(html, /<span class="secure-card__receipt-lock qcard__entry-lock" aria-hidden="true"><\/span><span>Secure input<\/span>/)
  assert.match(html, /<div class="qcard__header">Restart Möbius<\/div>/)
})

test('a card the agent carried on past with nothing answered draws nothing itself', () => {
  // The reply shows an "Asked a question" activity row in its place
  // (askedQuestionsActivity.test.js).
  assert.equal(renderToStaticMarkup(createElement(StaticQuestionCard, props({ disabled: true }))), '')
})

test('a refused card that is then carried past draws nothing, not a leftover refusal', async () => {
  const card = renderHook(QuestionCard, props({ onAnswer: () => Promise.reject(refusal(410)) }))
  try {
    editor(card.result.current, 'How formal?').props.onChange('Casual')
    click(submit(card.result.current))
    await tick()
    assert.ok(byClass(card.result.current, 'qcard__submit-error')[0])
    card.rerender(props({ disabled: true }))
    assert.equal(card.result.current, null)
  } finally { card.unmount() }
})

test('a partly answered card keeps its answers and says nothing about the entries left', () => {
  const html = renderToStaticMarkup(createElement(StaticQuestionCard, props({
    answeredMap: { 'How formal?': 'Casual' }, submittedOptions: {},
  })))
  assert.match(html, /How formal\?/)
  assert.doesNotMatch(html, /not answered|Publish the notes\?|Connect the mail service|Restart Möbius now\?/)
  assert.match(html, />Submitted</)
})

test('a tap on an incomplete Submit says what is missing in the button itself, moving nothing', () => {
  const card = renderHook(QuestionCard, props({ onAnswer: () => true }))
  try {
    click(submit(card.result.current))
    assert.equal(submit(card.result.current).props.children, 'Answer at least one question to submit')
    assert.match(hint(card.result.current).props.className, /chat__sr-only/, 'the hint line only announces it')
    editor(card.result.current, 'How formal?').props.onChange('Casual')
    assert.equal(submit(card.result.current).props.children, 'Submit 1 of 4 answers')
  } finally { card.unmount() }
})

test('a refusal appears under the button just pressed, never above it', async () => {
  const card = renderHook(QuestionCard, props({ onAnswer: () => Promise.reject(refusal(409)) }))
  try {
    editor(card.result.current, 'How formal?').props.onChange('Casual')
    click(submit(card.result.current))
    await tick()
    const all = elements(card.result.current)
    const error = byClass(card.result.current, 'qcard__submit-error')[0]
    assert.match(error.props.className, /qcard__submit-error--after/)
    assert.ok(all.indexOf(error) > all.indexOf(submit(card.result.current)))
  } finally { card.unmount() }
  const entryCard = renderHook(RestartEntry, restartProps({ onPress: () => Promise.reject(refusal(400)) }))
  try {
    click(byClass(entryCard.result.current, 'qcard__restart')[0])
    await tick()
    const all = elements(entryCard.result.current)
    assert.ok(all.indexOf(byClass(entryCard.result.current, 'qcard__submit-error')[0])
      > all.indexOf(byClass(entryCard.result.current, 'qcard__restart')[0]))
  } finally { entryCard.unmount() }
})


test('a live barring card retires the open list card, and a list card never bars', () => {
  assert.deepEqual(cardMarkersAfterLiveCard('key-card', { inputRequests: false }),
    { liveQuestionId: 'key-card', openRequestCardId: null })
  assert.deepEqual(cardMarkersAfterLiveCard('list-card', { inputRequests: true }),
    { openRequestCardId: 'list-card' })
})

test('an option without a description renders as its label alone', () => {
  const bare = { ...publish, options: [{ id: '0', label: 'Publish' }, { id: '1', label: 'Not now', description: 'Keep them as a draft.' }] }
  const html = renderToStaticMarkup(createElement(StaticQuestionCard, props({ questions: [bare], onAnswer: () => true })))
  assert.equal((html.match(/class="qcard__opt-desc"/g) || []).length, 1, 'only the described option has a description')
  assert.doesNotMatch(html, /undefined|aria-describedby=""/)
  const publishChoice = html.match(/<div class="qcard__opt-choice"[^>]*>(?:(?!qcard__opt-choice).)*?Publish/s)?.[0] || ''
  assert.ok(publishChoice, html)
  assert.doesNotMatch(publishChoice, /aria-describedby/)
})
