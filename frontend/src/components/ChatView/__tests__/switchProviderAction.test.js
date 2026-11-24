// The two ways out of a usage-limit pause that move the chat to another
// provider: "Switch provider" on the limit and credits cards, and "Continue" on
// the switch card that then ends the transcript in the pause card's place.
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { ownsSwitchResume, previousVisibleMessage } from '../recoveryCard.js'

const vite = await createSsrRenderServer()
const { default: MsgContent } = await vite.ssrLoadModule('/src/components/ChatView/MsgContent.jsx')
after(() => vite.close())

const pauseCard = pause => ({
  role: 'assistant', content: '',
  blocks: [{ type: 'error', message: '', resumable: true, pause }],
})
const usageLimit = pauseCard({ kind: 'usage_limit', check_at: '2099-09-14T12:00:00Z' })
const credits = pauseCard({ kind: 'credits', provider: 'codex' })

function render(msg, props = {}) {
  return renderToStaticMarkup(createElement(MsgContent, {
    msg, isLastMsg: true, onResume() {}, onChooseModel() {}, ...props,
  }))
}

test('a usage-limit card offers Switch provider beside its other actions', () => {
  const html = render(usageLimit, {
    autoResumeAvailable: true, onAutoResumeChange() {},
  })
  assert.match(html, />Switch provider<\/button>/)
  assert.match(html, />Auto-continue this chat<\/button>/)
  assert.match(html, />Try now<\/button>/)
  assert.equal(html.match(/chat__recovery-actionbar/g).length, 1, 'one action bar')
  const order = ['>Try now<', '>Switch provider<', '>Auto-continue this chat<'].map(label => html.indexOf(label))
  assert.deepEqual([...order].sort((a, b) => a - b), order,
    'retry, then switch, then the quiet auto-continue, so the keyboard order is the visual order')
})

test('a usage-limit card without the auto-continue control still offers Switch provider', () => {
  const html = render(usageLimit)
  assert.match(html, />Switch provider<\/button>/)
  assert.doesNotMatch(html, /Auto-continue this chat/)
})

test('a credits card offers Switch provider and keeps Resume', () => {
  const html = render(credits)
  assert.match(html, />Switch provider<\/button>/)
  assert.match(html, />Resume<\/button>/)
})

for (const kind of ['model_capacity', 'memory', 'storage', 'processes']) {
  test(`a ${kind} wait never offers Switch provider`, () => {
    const html = render(pauseCard({ kind, check_at: '2099-09-14T12:00:00Z' }))
    assert.doesNotMatch(html, /Switch provider/)
  })
}

test('Switch provider is absent when the model picker is not available', () => {
  assert.doesNotMatch(render(usageLimit, { onChooseModel: undefined }), /Switch provider/)
  assert.doesNotMatch(render(credits, { onChooseModel: undefined }), /Switch provider/)
})

test('Switch provider belongs only to the card that owns recovery', () => {
  assert.doesNotMatch(render(usageLimit, { isLastMsg: false }), /Switch provider/)
  assert.doesNotMatch(render(usageLimit, { onResume: undefined }), /Switch provider/)
})

test('Switch provider is disabled while a provider switch is in progress', () => {
  const html = render(usageLimit, { submissionBlocked: true })
  assert.match(html, /<button[^>]*disabled=""[^>]*>Switch provider<\/button>/)
})

test('a provider refusal still says Switch model, never Switch provider', () => {
  const html = render(pauseCard({ kind: 'provider_refusal', provider: 'claude' }), {
    onRefusalFreshSession() {},
  })
  assert.match(html, />Switch model<\/button>/)
  assert.doesNotMatch(html, /Switch provider/)
})

const switchRow = (extra = {}) => ({
  role: 'assistant', kind: 'compaction', handover: 'direct', to_provider: 'codex',
  content: 'Digest: the saved summary.', ...extra,
})
const switchProps = (props = {}) => ({
  previousMsg: usageLimit, onChooseModel: undefined, ...props,
})

test('the switch card after a paused turn offers Continue under the card', () => {
  const html = render(switchRow(), switchProps())
  assert.match(html, />Switched to OpenAI Codex</)
  assert.match(html, /class="chat__resume chat__recovery-action"[^>]*>Continue<\/button>/)
  assert.ok(html.indexOf('Switched to') < html.indexOf('>Continue<'))
})

test('Continue shows its pending, reconnecting and error states like Resume does', () => {
  const pending = render(switchRow(), switchProps({ resumeState: { pending: true } }))
  assert.match(pending, /disabled=""[^>]*aria-busy="true"[^>]*>Continuing…<\/button>/)
  const waiting = render(switchRow(), switchProps({ resumeState: { unavailable: true } }))
  assert.match(waiting, /disabled=""[^>]*>Reconnecting…<\/button>/)
  const failed = render(switchRow(), switchProps({ resumeState: { error: 'Could not continue.' } }))
  assert.match(failed, /role="alert">Could not continue\.</)
  const blocked = render(switchRow(), switchProps({ submissionBlocked: true }))
  assert.match(blocked, /disabled=""[^>]*title="Wait for the provider switch to finish\."[^>]*>Continue</)
})

test('Continue only appears under the conditions that make it safe', () => {
  const absent = [
    ['a row without a handover', switchRow({ handover: undefined }), switchProps()],
    ['a model-written summary', switchRow({ handover: undefined, to_provider: undefined }), switchProps()],
    ['a switch row that is not the tail', switchRow(), switchProps({ isLastMsg: false })],
    ['a view that cannot resume', switchRow(), switchProps({ onResume: undefined })],
    ['no previous row', switchRow(), switchProps({ previousMsg: null })],
    ['a previous row that was not paused', switchRow(), switchProps({
      previousMsg: { role: 'assistant', content: 'Done.', blocks: [{ type: 'text', content: 'Done.' }] },
    })],
    ['a previous pause that is not resumable', switchRow(), switchProps({
      previousMsg: { role: 'assistant', blocks: [{ type: 'error', pause: { kind: 'usage_limit' } }] },
    })],
  ]
  for (const [label, msg, props] of absent) {
    assert.doesNotMatch(render(msg, props), /Continue<\/button>/, label)
  }
})

const ownership = {
  msg: switchRow(), previousMsg: usageLimit, isLastMessage: true, canResume: true,
}

test('a switch card owns Resume only as the tail after a resumable pause', () => {
  assert.equal(ownsSwitchResume(ownership), true)
  assert.equal(ownsSwitchResume({ ...ownership, isLastMessage: false }), false)
  assert.equal(ownsSwitchResume({ ...ownership, canResume: false }), false)
  assert.equal(ownsSwitchResume({ ...ownership, questionOwnsTurn: true }), false,
    'an unanswered question owns the turn ahead of the switch')
  assert.equal(ownsSwitchResume({ ...ownership, msg: switchRow({ handover: undefined }) }), false)
  assert.equal(ownsSwitchResume({ ...ownership, msg: { role: 'assistant', blocks: [] } }), false)
  assert.equal(ownsSwitchResume({ ...ownership, previousMsg: null }), false)
  assert.equal(ownsSwitchResume({ ...ownership, previousMsg: { blocks: [] } }), false)
  assert.equal(ownsSwitchResume({
    ...ownership,
    previousMsg: { blocks: [{ type: 'error', resumable: true }, { type: 'text' }] },
  }), false, 'only the last block of the previous row can be the pause')
})

test('the row before the tail is the nearest visible one', () => {
  const rows = [{ id: 1 }, { id: 2 }, { id: 3, hidden: true }, { id: 4 }]
  assert.equal(previousVisibleMessage(rows, 3).id, 2)
  assert.equal(previousVisibleMessage(rows, 1).id, 1)
  assert.equal(previousVisibleMessage(rows, 0), null)
})

test('a second switch in a row still finds the paused turn to continue', () => {
  const switched = { role: 'assistant', kind: 'compaction', handover: 'direct' }
  const rows = [usageLimit, { ...switched, to_provider: 'codex' }, { ...switched, to_provider: 'claude' }]
  const previousMsg = previousVisibleMessage(rows, 2)

  assert.equal(previousMsg, usageLimit)
  assert.equal(ownsSwitchResume({
    msg: rows[2], previousMsg, isLastMessage: true, canResume: true,
  }), true)
})
