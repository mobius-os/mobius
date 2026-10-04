/* Credit exhaustion is an informational, manually resumed chat pause. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { ownsRecoveryAction } from '../recoveryCard.js'
import { supersedeResumedPauseBlocks } from '../chatRuntimeState.js'
import { streamItemsToAssistantPayload } from '../streamPromotion.js'

const vite = await createServer({
  appType: 'custom', logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { default: ErrorCard, errorCardViewModel } = await vite.ssrLoadModule('/src/components/ChatView/ErrorCard.jsx')
const { default: MsgContent } = await vite.ssrLoadModule('/src/components/ChatView/MsgContent.jsx')
after(() => vite.close())

// The backend classifies the provider rejection once and stamps this pause.
const creditBlock = {
  type: 'error',
  message: 'Your workspace is out of credits. Add credits to continue.',
  resumable: true,
  pause: { kind: 'credits', provider: 'codex' },
}
const message = { role: 'assistant', content: '', blocks: [creditBlock] }

test('a credits pause renders as a calm card without rewriting history', () => {
  const original = structuredClone(creditBlock)
  const vm = errorCardViewModel(creditBlock)
  assert.equal(vm.benign, true)
  assert.equal(vm.parked, false)
  assert.equal(vm.label, 'Credits needed')
  const html = renderToStaticMarkup(createElement(ErrorCard, { block: creditBlock }))
  assert.match(html, /chat__text--parked/)
  assert.match(html, /Your progress is saved/)
  assert.match(html, /Add credits to your workspace or choose another provider, then Resume/)
  assert.doesNotMatch(html, /role="alert"|>Error<|automatically|retry check|Rate limit/)
  assert.deepEqual(creditBlock, original)
})

test('live stream and persisted credit blocks have identical informative bodies', () => {
  const payload = streamItemsToAssistantPayload([{ ...creditBlock, seq: 1 }])
  assert.equal(errorCardViewModel(payload.blocks[0]).credits, true)
  const render = block => renderToStaticMarkup(createElement(ErrorCard, { block }))
  assert.equal(render(payload.blocks[0]), render(creditBlock))
})

test('only the actionable transcript tail offers Resume, never automatic paid recovery', () => {
  const render = props => renderToStaticMarkup(createElement(MsgContent, {
    msg: message, isLastMsg: true, onResume() {},
    autoResumeAvailable: true, autoResumeEnabled: true, onAutoResumeChange() {},
    ...props,
  }))
  const html = render({})
  assert.match(html, />Resume<\/button>/)
  assert.doesNotMatch(html, /auto-continue|Try now/)
  assert.doesNotMatch(render({ isLastMsg: false }), />Resume<\/button>/)
  assert.doesNotMatch(render({ onResume: undefined }), />Resume<\/button>/)
  const ownership = { block: creditBlock, entryIndex: 0, lastEntryIndex: 0, isLastMessage: true, canResume: true }
  assert.equal(ownsRecoveryAction(ownership), true)
  assert.equal(ownsRecoveryAction({ ...ownership, questionOwnsTurn: true }), false)
  assert.equal(ownsRecoveryAction({ ...ownership, lastEntryIndex: 1 }), false)
})

test('credit resume keeps existing pending and unavailable button states', () => {
  for (const [resumeState, label] of [[{ pending: true }, 'Resuming…'], [{ unavailable: true }, 'Reconnecting…']]) {
    const html = renderToStaticMarkup(createElement(MsgContent, {
      msg: message, isLastMsg: true, onResume() {}, resumeState,
    }))
    assert.match(html, new RegExp(`>${label}<\\/button>`))
    assert.match(html, /disabled=""/)
  }
})

test('accepted resume supersedes the old credit pause only in the render projection', () => {
  const messages = [message, { role: 'user', kind: 'continuation', content: '' }]
  const projected = supersedeResumedPauseBlocks(messages)
  assert.equal(projected[0].hidden, true)
  assert.deepEqual(projected[0].blocks, [])
  assert.deepEqual(messages[0].blocks, [creditBlock])
  assert.equal(supersedeResumedPauseBlocks([message])[0], message)
})

test('the card follows the backend pause kind, never the error text', () => {
  for (const block of [
    { type: 'error', message: 'Payment authorization failed.' },
    { type: 'error', message: creditBlock.message },
  ]) {
    const vm = errorCardViewModel(block)
    assert.equal(vm.credits, false)
    assert.equal(vm.benign, false)
    assert.equal(vm.label, 'Error')
  }
})
