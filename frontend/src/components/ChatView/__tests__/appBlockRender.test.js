import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import AppBlock from '../markdown/AppBlock.jsx'
import { AppBlockView } from '../markdown/AppBlockParts.jsx'
import { PassiveBlockView, SessionConfirmation, SessionControls, passiveBlockMode } from '../markdown/PassiveAppBlock.jsx'
import { sessionState } from '../markdown/appBlockSession.js'
import { appBlockFromToken } from '../markdown/appBlock.js'
import { appQueries } from '../../../hooks/queries.js'

const installed = [{ id: 80, slug: 'example', name: 'Example' }]

function render(value, apps = installed) {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify(value) })
  const client = new QueryClient()
  if (apps) client.setQueryData(appQueries.list.key, apps)
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(AppBlock, { block })))
}

const base = { app: 'example', intent: 'open:item-42', title: 'Item 42' }

test('a block renders its snapshot and app link without mounting the app', () => {
  const html = render({ ...base, facts: [{ label: 'Author', value: 'octocat' },
    { label: 'Source', value: 'owner/repo', href: 'https://github.com/owner/repo' }],
  badges: [{ label: 'Ready', tone: 'success' }] })
  assert.match(html, /<strong>Item 42<\/strong>/)
  assert.match(html, /<dt>Author<\/dt><dd>octocat<\/dd>/)
  assert.match(html, /<a href="https:\/\/github\.com\/owner\/repo" target="_blank" rel="noopener noreferrer">owner\/repo<\/a>/)
  assert.match(html, /md-app-block__badge is-success">Ready</)
  assert.match(html, /href="\/shell\/\?app=example&amp;intent=open%3Aitem-42"[^>]*>Open in Example/)
  assert.match(html, /aria-expanded="false"[^>]*>.*Show details here<\/button>/)
  assert.doesNotMatch(html, /md-app-block__view|<iframe/)
})

test('an action and the expand control only name views the reader can open', () => {
  const html = render({ ...base, expand_label: 'Review here', action: { label: 'Send', intent: 'send:item-42' } })
  assert.match(html, />Review here<\/button>/)
  assert.match(html, /class="md-app-block__action" aria-pressed="false">Send<\/button>/)
  assert.doesNotMatch(html, /md-app-block__view/)
})

test('link-only blocks have no expand control or action', () => {
  const html = render({ ...base, inline: false, action: { label: 'Send', intent: 'send:1' } })
  assert.doesNotMatch(html, /md-app-block__toggle|md-app-block__action/)
  assert.match(html, /Open in Example/)
})

test('items render as generic linked rows with their own badges, facts and actions', () => {
  const html = render({ ...base, title: 'Ready to contribute', action: { label: 'Contribute all', intent: 'send-batch:sel-1' },
    items: [
      { title: 'First change', intent: 'open:a', badges: [{ label: 'All clear', tone: 'success' }],
        facts: [{ label: 'Repository', value: 'owner/repo' }], action: { label: 'Contribute', intent: 'send:a' } },
      { title: 'Second change', intent: 'open:b' },
    ] })
  assert.match(html, /intent=open%3Aa"[^>]*>First change</)
  assert.match(html, /intent=open%3Ab"[^>]*>Second change</)
  assert.match(html, /is-success">All clear</)
  assert.equal((html.match(/class="md-app-block__action"/g) || []).length, 2)
  assert.match(html, />Contribute all<\/button>/)
  // The platform has no app-specific widget: no PR-row markup or label colours.
  assert.doesNotMatch(html, /md-app-pull|--md-label/)
})

test('every shape explains a missing or deleted app and offers no view', () => {
  const shapes = [base, { ...base, inline: false },
    { ...base, action: { label: 'Send', intent: 'send:1' } },
    { ...base, items: [{ title: 'Row', intent: 'open:1', action: { label: 'Go', intent: 'go:1' } }] }]
  for (const apps of [[], [{ ...installed[0], deleted_at: '2026-10-01T00:00:00Z' }]]) {
    for (const shape of shapes) {
      const html = render(shape, apps)
      assert.match(html, /example is not installed\. The saved snapshot remains here\./)
      assert.doesNotMatch(html, /md-app-block__toggle|md-app-block__action|md-app-block__view/)
    }
  }
  assert.match(render(base, null), /Checking installed apps…/)
})

test('every block says its content comes from the conversation', () => {
  for (const shape of [base, { ...base, inline: false }, { ...base, badges: [{ label: 'Approved', tone: 'success' }] }]) {
    for (const apps of [installed, []]) {
      assert.match(render(shape, apps), /<header>[^]*<\/header><p class="md-app-block__source">From this conversation<\/p>/)
    }
  }
})

test('the explicit view mounts the ordinary app canvas lazily', () => {
  const html = renderToStaticMarkup(createElement(AppBlockView, { app: installed[0], intent: 'open:1', height: 480, onHostRequest() {} }))
  assert.match(html, /class="md-app-block__view" style="height:480px"/)
  assert.match(html, /Opening Example…/)
})

const admitted = [{ ...installed[0], capability_contract: { runtime: { 'chat.blocks.passive': { version: 1 } } },
  passive_block_module_digest: 'a'.repeat(64) }]
const sessionBlock = { ...base, interaction: 'inline', action: { label: 'Contribute all', intent: 'send-batch:sel-1' },
  badges: [{ label: 'All clear', tone: 'success' }] }

test('an admitted inline session waits for its document instead of showing saved actions', () => {
  const html = render(sessionBlock, admitted)
  assert.match(html, /md-app-block__pending" role="status">Loading…</)
  assert.doesNotMatch(html, />Contribute all<\/button>|md-app-block__toggle|<iframe/)
})

test('an inline session for an app without the admitted capability is a static block', () => {
  for (const apps of [installed, [{ ...admitted[0], passive_block_module_digest: null }]]) {
    const html = render(sessionBlock, apps)
    assert.match(html, /aria-pressed="false">Contribute all<\/button>/)
    assert.doesNotMatch(html, /Loading…/)
  }
})

const controls = (state, locked = false, confirmArmed = true) => renderToStaticMarkup(createElement(SessionControls,
  { target: { label: 'Contribute all', intent: 'send-batch:sel-1' }, state, locked, confirmArmed, onEvent() {} }))
const live = fields => ({ label: '', disabled: false, busy: false, confirming: false, confirmation: null, hidden: false,
  note: '', tone: 'neutral', status: '', statusTone: 'neutral', links: [], ...fields })

test('Confirm takes the action\'s own fixed-size place with Not now beside it', () => {
  const idle = controls(live({ label: 'Contribute all', status: 'Ready', links: [{ label: 'View PR', url: 'https://example.com/pr/1' }] }))
  assert.match(idle, /md-app-block__status is-neutral">Ready</)
  assert.match(idle, /href="https:\/\/example\.com\/pr\/1"/)
  assert.match(idle, /class="md-app-block__action"[^>]*>Contribute all<\/button>/)
  const confirming = controls(live({ confirming: true, confirmation: [{ title: 'Exact', facts: [] }] }))
  assert.match(confirming, />Not now<\/button><button type="button" class="md-app-block__action">Confirm<\/button>/)
  assert.equal((confirming.match(/class="md-app-block__action"/g) || []).length, 1)
})

test('Confirm is shown disabled in place until it has been armed', () => {
  const html = controls(live({ confirming: true, confirmation: [{ title: 'Exact', facts: [] }] }), false, false)
  assert.match(html, /class="md-app-block__action" disabled="">Confirm/)
  assert.doesNotMatch(html, /class="md-app-block__cancel" disabled/)
})

test('Confirm is disabled without the complete app list or while an event awaits acknowledgement', () => {
  assert.match(controls(live({ confirming: true, disabled: true })), /class="md-app-block__action" disabled="">Confirm/)
  const locked = controls(live({ confirming: true, confirmation: [{ title: 'Exact', facts: [] }] }), true)
  assert.match(locked, /class="md-app-block__cancel" disabled="">Not now/)
  assert.match(locked, /class="md-app-block__action" disabled="">Confirm/)
  assert.match(controls(live({ busy: true, disabled: true, label: 'Sending…' })), /disabled="" aria-busy="true"[^>]*>Sending…/)
  assert.equal(controls(live({ hidden: true })), '')
})

test('the confirmation lists exactly the app-supplied items and marks conversation text', () => {
  const html = renderToStaticMarkup(createElement(SessionConfirmation, { appName: 'Example', action: live({ confirming: true,
    confirmation: [{ title: 'owner/repo: Fix it', facts: [{ label: 'Destination', value: 'owner/repo → main' }] },
      { title: 'Second', facts: [] }] }) }))
  assert.match(html, /Example will act on:/)
  assert.equal((html.match(/<li>/g) || []).length, 2)
  assert.match(html, /owner\/repo → main/)
  assert.match(html, /The saved title and details above come from the conversation\. The list below comes from Example\.[^]*will act on:/,
    'the provenance note precedes, and so does not disclaim, the app list')
  const incomplete = renderToStaticMarkup(createElement(SessionConfirmation, { appName: 'Example',
    action: live({ confirming: true, disabled: true, note: 'Open the app to review this action. Its full current identity is unavailable here.' }) }))
  assert.match(incomplete, /Its full current identity is unavailable here/)
  assert.doesNotMatch(incomplete, /<li>/)
})

function passiveView(actions) {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...base, interaction: 'inline',
    action: { label: 'Send all', intent: 'send-batch:sel-1' },
    items: [{ title: 'Row A', intent: 'open:a', action: { label: 'Send A', intent: 'send:a' } },
      { title: 'Row B', intent: 'open:b', action: { label: 'Send B', intent: 'send:b' } }] }) })
  const state = sessionState({ type: 'moebius:app-block-state', sessionId: 's', actions },
    's', new Set(['send-batch:sel-1', 'send:a', 'send:b']))
  return renderToStaticMarkup(createElement(PassiveBlockView, { block, app: admitted[0], state, locked: false,
    confirmArmed: true, onEvent() {} }))
}
const target = title => ({ confirming: true, confirmation: [{ title, facts: [{ label: 'Version', value: title }] }] })
const enabledConfirms = html => (html.match(/class="md-app-block__action">Confirm</g) || []).length

test('the one confirming action shows its own app list beside the only enabled Confirm', () => {
  const html = passiveView([{ key: 'send-batch:sel-1', label: 'Send all' }, { key: 'send:a', ...target('Target A') }, { key: 'send:b', label: 'Send B' }])
  assert.equal(enabledConfirms(html), 1)
  assert.match(html, /Target A/)
})

test('several confirming actions are ambiguous: no Confirm is enabled and no list is shown', () => {
  for (const first of [target('Target A'), { confirming: true, confirmation: [] }, { ...target('Target A'), hidden: true }]) {
    const html = passiveView([{ key: 'send:a', ...first }, { key: 'send:b', ...target('Target B') }])
    if (first.hidden) {
      // A hidden control cannot confirm, so the visible second one is the only confirmation.
      assert.equal(enabledConfirms(html), 1)
      assert.match(html, /Target B/)
      assert.doesNotMatch(html, /Target A/)
      continue
    }
    assert.equal(enabledConfirms(html), 0)
    assert.doesNotMatch(html, /Target A|Target B/)
    assert.match(html, /More than one is asking for confirmation at once/)
  }
})

test('a static view the reader opened stays until the reader closes it, even when a slot frees', () => {
  const base = { supported: true, near: true, retained: false }
  assert.equal(passiveBlockMode({ ...base, granted: false, staticOpen: false }), 'static')
  assert.equal(passiveBlockMode({ ...base, granted: true, staticOpen: true }), 'static', 'a freed slot keeps the open view')
  assert.equal(passiveBlockMode({ ...base, granted: true, staticOpen: false }), 'passive', 'closing it may go passive again')
  assert.equal(passiveBlockMode({ ...base, supported: false, granted: true, staticOpen: false }), 'static')
  assert.equal(passiveBlockMode({ ...base, granted: false, staticOpen: true, retained: true }), 'passive', 'an unresolved owner keeps its session')
})

test('once started, the passive session owns the block across admission changes and decides held or static itself', async () => {
  const { appBlockRoute } = await import('../markdown/AppBlock.jsx')
  const live = { requested: true, installed: true }
  assert.equal(appBlockRoute({ ...live, admitted: true, started: false }), 'passive')
  assert.equal(appBlockRoute({ ...live, admitted: false, started: false }), 'static', 'a never-admitted app stays static')
  assert.equal(appBlockRoute({ ...live, admitted: false, started: true }), 'passive', 'the owner stays mounted when admission goes')
  assert.equal(appBlockRoute({ ...live, installed: false, admitted: false, started: true }), 'static')
  assert.equal(appBlockRoute({ ...live, requested: false, admitted: true, started: true }), 'static')
  // Inside the owner, from its own synchronous state:
  const base = { supported: true, near: true, granted: true, staticOpen: false }
  assert.equal(passiveBlockMode({ ...base, admitted: false, retained: true }), 'passive', 'held')
  assert.equal(passiveBlockMode({ ...base, admitted: false, retained: false }), 'static', 'settled: static form')
  assert.equal(passiveBlockMode({ ...base, admitted: true, retained: false }), 'passive')
})

test('a held session tells the reader to check the result and offers no control', async () => {
  const { ADMISSION_WITHDRAWN, sessionLocked } = await import('../markdown/PassiveAppBlock.jsx')
  const state = sessionState({ type: 'moebius:app-block-state', sessionId: 's', retain: true,
    actions: [{ key: 'send-batch:sel-1', label: 'Send all', busy: true }] }, 's', new Set(['send-batch:sel-1']))
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...base, interaction: 'inline',
    action: { label: 'Send all', intent: 'send-batch:sel-1' } }) })
  const session = { state, awaitingAck: false }
  assert.equal(sessionLocked(session, false), true)
  assert.equal(sessionLocked({ state: null, awaitingAck: false }, false), true, 'locked even with no busy action')
  assert.equal(sessionLocked({ state: null, awaitingAck: false }, true), false)
  const html = renderToStaticMarkup(createElement(PassiveBlockView, { block, app: admitted[0], state,
    locked: sessionLocked(session, false), confirmArmed: false, notice: ADMISSION_WITHDRAWN('Example'), onEvent() {} }))
  assert.match(html, /Example can no longer run in this chat\. Open Example to check the result of the last action/)
  assert.doesNotMatch(html, /class="md-app-block__action">/, 'no enabled control')
})

const noExpand = { ...base, expand: false, action: { label: 'Publish', intent: 'send:all' },
  items: [{ title: 'First change', intent: 'open:a', facts: [{ label: 'Project', value: 'example/project' }, { label: 'Lines', value: '+12 −3' }] }] }

test('expand:false offers no inline view; the action opens the app at its intent like a link', () => {
  const html = render(noExpand)
  assert.doesNotMatch(html, /md-app-block__toggle|Show details here|aria-pressed|md-app-block__view/)
  assert.match(html, /<a class="md-app-block__action" href="\/shell\/\?app=example&amp;intent=send%3Aall">Publish<\/a>/)
  assert.match(html, /intent=open%3Aitem-42"[^>]*>Open in Example/)
  assert.match(html, /intent=open%3Aa"[^>]*>First change</)
  // The default keeps the expand control and the in-place view action.
  const expandable = render({ ...noExpand, expand: true })
  assert.match(expandable, /Show details here/)
  assert.match(expandable, /<button type="button" class="md-app-block__action" aria-pressed="false">Publish<\/button>/)
})

test('a passive block that falls back to static (settled or withdrawn) keeps expand:false', async () => {
  const PassiveAppBlock = (await import('../markdown/PassiveAppBlock.jsx')).default
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...noExpand, interaction: 'inline' }) })
  const html = renderToStaticMarkup(createElement(PassiveAppBlock, { block, app: admitted[0], admitted: false }))
  assert.match(html, /<a class="md-app-block__action" href="[^"]*intent=send%3Aall">Publish<\/a>/)
  assert.doesNotMatch(html, /md-app-block__toggle|Show details here|aria-pressed/)
})
