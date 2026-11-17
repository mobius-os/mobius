/* A helper's recovery card offers only what its delegation can retry and links the parent for the rest. */
import { readFileSync } from 'node:fs'
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { default: MsgContent } = await vite.ssrLoadModule(
  '/src/components/ChatView/MsgContent.jsx',
)
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

after(() => vite.close())

const ACTION = /class="chat__resume chat__recovery-action"/
const PARENT_LINK = /This helper continues from its <a class="chat__offline-note-link" href="\/shell\?chat=parent%2Fa"[^>]*>parent chat<\/a>\./

function card(pause, props = {}) {
  return renderToStaticMarkup(createElement(MsgContent, {
    msg: { role: 'assistant', content: '', blocks: [{
      type: 'error', resumable: true, message: 'Paused.',
      ...(pause ? { pause } : {}),
    }] },
    isLastMsg: true,
    onResume() {},
    resumeState: {},
    canContinueAnyway: true,
    helperChat: true,
    helperParentChatId: 'parent/a',
    ...props,
  }))
}

test('a helper keeps Try now on the usage-limit park its delegation can retry', () => {
  const html = card({ kind: 'usage_limit', check_at: '2099-09-14T12:00:00Z' })
  assert.match(html, />Try now<\/button>/)
  assert.doesNotMatch(html, /This helper continues from its/)
})

for (const [name, pause, props, refused] of [
  ['rate-limit pause', { kind: 'rate_limit', check_at: '2099-09-14T12:00:00Z' }, {}, /Try now/],
  ['restart note', { kind: 'restart' }, { handoff: { kind: 'recovery' } }, />Resume</],
  ['crash note', null, {}, />Resume</],
  ['out-of-credits note', { kind: 'credits' }, {}, />Resume</],
  ['low-storage wait', { kind: 'storage' }, { handoff: { kind: 'automatic', reason: 'storage' } }, /Continue anyway|If the disk fills/],
  ['process wait', { kind: 'processes' }, { handoff: { kind: 'recovery', reason: 'processes' } }, /Continue anyway|If the limit is reached/],
]) {
  test(`a helper's ${name} links its parent instead of offering an action the server refuses`, () => {
    const html = card(pause, props)
    assert.doesNotMatch(html, ACTION)
    assert.doesNotMatch(html, refused)
    assert.match(html, PARENT_LINK)
    // The same card in an ordinary chat keeps its action.
    const ordinary = card(pause, { ...props, helperChat: false, helperParentChatId: null })
    assert.match(ordinary, ACTION)
    assert.doesNotMatch(ordinary, /This helper continues from its/)
  })
}

test('a helper whose owner chat is gone still says where recovery lives, without a dead link', () => {
  const html = card({ kind: 'restart' }, { handoff: { kind: 'recovery' }, helperParentChatId: null })
  assert.doesNotMatch(html, ACTION)
  assert.match(html, /This helper continues from its parent chat\./)
  assert.doesNotMatch(html, /<a /)
})

test('only the tail card that owns recovery names the parent', () => {
  assert.doesNotMatch(card({ kind: 'restart' }, { isLastMsg: false }), /This helper continues from its/)
})

test('ChatView sends every resume, transcript and Goal rail, through the settings-aware owner', () => {
  assert.match(chatView, /sendRecoveryResume\(\{\s*helperDelegationId, settingsSaved, request: apiFetch, send: sendAfterSettingsSaved,/)
  assert.match(chatView, /const sendAfterSettingsSaved = useCallback\(async \(text, attachments, options\) => \{\s*await settingsSaved\(\)/)
  assert.match(chatView, /helperChat=\{!!helperDelegationId\}\s*helperParentChatId=\{helperParentChatId\}/)
  assert.doesNotMatch(chatView, /retryHelperRecovery/)
})
