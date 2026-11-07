import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import AppBlock from '../markdown/AppBlock.jsx'
import { AppBlockView } from '../markdown/AppBlockParts.jsx'
import { AskControl, PassiveBlockView, SessionControls, blockAsk, passiveBlockMode } from '../markdown/PassiveAppBlock.jsx'
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
      assert.match(html, /The example app is not installed\. The saved snapshot remains here\./)
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
  assert.match(idle, /md-app-block__pill is-neutral">Ready</, 'status is a pill, not a coloured sentence')
  assert.match(idle, /class="md-app-block__link" href="https:\/\/example\.com\/pr\/1" target="_blank" rel="noopener noreferrer" aria-label="View PR, opens in a new tab">View PR<svg/)
  assert.match(idle, /class="md-app-block__action"[^>]*>Contribute all<\/button>/)
  const confirming = controls(live({ confirming: true, confirmation: [{ title: 'Exact', facts: [] }] }))
  assert.match(confirming, />Not now<\/button><button type="button" class="md-app-block__action">Confirm<\/button>/)
  assert.equal((confirming.match(/class="md-app-block__action"/g) || []).length, 1)
})

test('Confirm is shown disabled in place until it has been armed', () => {
  const html = controls(live({ confirming: true, confirmation: [{ title: 'Exact', facts: [] }] }), false, false)
  assert.match(html, /class="md-app-block__action" disabled="" aria-describedby="[^"]+">Confirm/)
  assert.doesNotMatch(html, /class="md-app-block__cancel" disabled/)
})

test('Confirm is disabled without the complete app list or while an event awaits acknowledgement', () => {
  assert.match(controls(live({ confirming: true, disabled: true })), /class="md-app-block__action" disabled="">Confirm/)
  const locked = controls(live({ confirming: true, confirmation: [{ title: 'Exact', facts: [] }] }), true)
  assert.match(locked, /class="md-app-block__cancel" disabled="">Not now/)
  assert.match(locked, /class="md-app-block__action" disabled="">Confirm/)
  assert.match(controls(live({ busy: true, disabled: true, label: 'Sending…' })),
    /disabled="" aria-busy="true"[^>]*><span class="md-app-block__spinner" aria-hidden="true"><\/span>Sending…/)
  assert.equal(controls(live({ hidden: true })), '')
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

const rowTitles = html => [...html.matchAll(/class="md-app-block__item-title"[^>]*>([^<]*)</g)].map(match => match[1])

test('the block\'s own action shows the app list as the block\'s rows, with nothing added', () => {
  const html = passiveView([{ key: 'send-batch:sel-1', confirming: true, confirmation: [
    { title: 'owner/repo: Fix it', facts: [{ label: 'Project', value: 'owner/repo' }, { label: 'Change', value: '1 file · +2 −0' }] },
    { title: 'Second', facts: [] }] }, { key: 'send:a', label: 'Send A' }, { key: 'send:b', label: 'Send B' }])
  assert.deepEqual(rowTitles(html), ['owner/repo: Fix it', 'Second'], 'the app rows replace every conversation row')
  assert.doesNotMatch(html, /Row A|Row B|open%3Aa/)
  assert.match(html, /<span class="md-app-block__item-title">owner\/repo: Fix it<\/span>/, 'app rows link nowhere')
  assert.match(html, /<dt>Project<\/dt><dd>owner\/repo<\/dd><\/div><div><dt>Change<\/dt><dd>1 file · \+2 −0<\/dd>/)
  assert.doesNotMatch(html, /come from the conversation|will act on|md-app-block__confirmation/, 'no explanation section')
  assert.doesNotMatch(html, /From this conversation/, 'not even the caption')
  // Both ends of the list are observed: the start in its first row, the end in its last.
  const rows = html.split('<li class="is-confirmation">').slice(1)
  assert.equal(rows.length, 2, 'app rows are marked as confirmation rows, which always keep their labels')
  assert.match(rows[0], /^<span class="md-app-block__list-edge is-start"/)
  assert.match(rows[1], /md-app-block__list-edge is-end" aria-hidden="true"><\/span><\/li>/)
  assert.match(html, /<footer><div class="md-app-block__session">[^]*>Not now<\/button><button type="button" class="md-app-block__action">Confirm</)
  assert.equal(enabledConfirms(html), 1)
})

test('a confirming item owns the whole block: its app rows and one Confirm, nothing else', () => {
  const html = passiveView([{ key: 'send-batch:sel-1', label: 'Send all' }, { key: 'send:a', ...target('Target A') }, { key: 'send:b', label: 'Send B' }])
  assert.equal(enabledConfirms(html), 1)
  assert.deepEqual(rowTitles(html), ['Target A'])
  assert.doesNotMatch(html, /Row B|Send all|Send B/, 'no other row or control')
  assert.match(html, /<footer>[^]*>Not now<\/button><button type="button" class="md-app-block__action">Confirm</)
})

// The reviewer's cases: actions placed twice, and saved text that contradicts the app.
function fenceView(fence, actions, keys) {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...base, interaction: 'inline', ...fence }) })
  const state = sessionState({ type: 'moebius:app-block-state', sessionId: 's', actions }, 's', new Set(keys))
  return renderToStaticMarkup(createElement(PassiveBlockView, { block, app: admitted[0], state, locked: false, confirmArmed: true, onEvent() {} }))
}
const real = { confirming: true, confirmation: [{ title: 'REAL target', facts: [{ label: 'Project', value: 'owner/real' }] }] }

test('distinct items sharing one action key yield one Confirm beside only the app rows', () => {
  const html = fenceView({ items: [
    { title: 'Harmless A', intent: 'open:a', facts: [{ label: 'Project', value: 'owner/fake' }], action: { label: 'Send', intent: 'send:a' } },
    { title: 'FAKE harmless target', intent: 'open:b', action: { label: 'Send', intent: 'send:a' } }] },
  [{ key: 'send:a', ...real }], ['send:a'])
  assert.equal(enabledConfirms(html), 1)
  assert.equal((html.match(/>Confirm</g) || []).length, 1)
  assert.deepEqual(rowTitles(html), ['REAL target'])
  assert.doesNotMatch(html, /FAKE|Harmless|owner\/fake|From this conversation/)
})

test('the same key on the block and an item yields one Confirm', () => {
  const html = fenceView({ action: { label: 'Send all', intent: 'send:a' },
    items: [{ title: 'FAKE row', intent: 'open:b', action: { label: 'Send', intent: 'send:a' } }] },
  [{ key: 'send:a', ...real }], ['send:a'])
  assert.equal((html.match(/>Confirm</g) || []).length, 1)
  assert.deepEqual(rowTitles(html), ['REAL target'])
})

test('saved facts that contradict the app are not shown while confirming, and return with Not now', () => {
  const fence = { facts: [{ label: 'Project', value: 'owner/saved' }], action: { label: 'Send all', intent: 'send:a' },
    items: [{ title: 'Saved row', intent: 'open:b', facts: [{ label: 'Project', value: 'owner/saved' }] }] }
  const confirming = fenceView(fence, [{ key: 'send:a', ...real }], ['send:a'])
  assert.doesNotMatch(confirming, /owner\/saved|Saved row/)
  assert.match(confirming, /owner\/real/)
  const cancelled = fenceView(fence, [{ key: 'send:a', label: 'Send all' }], ['send:a'])
  assert.match(cancelled, /owner\/saved[^]*Saved row/, 'the original card returns')
  assert.match(cancelled, /From this conversation/)
})

test('a waiting Confirm tells assistive technology why, and the rows are announced politely', () => {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...base, interaction: 'inline',
    action: { label: 'Send all', intent: 'send:a' } }) })
  const state = sessionState({ type: 'moebius:app-block-state', sessionId: 's', actions: [{ key: 'send:a', ...real }] }, 's', new Set(['send:a']))
  const view = armed => renderToStaticMarkup(createElement(PassiveBlockView, { block, app: admitted[0], state, locked: false,
    confirmArmed: armed, onEvent() {} }))
  const waiting = view(false)
  const id = /aria-describedby="([^"]+)"/.exec(waiting)?.[1]
  assert.ok(id, 'the disabled Confirm is described')
  assert.match(waiting, new RegExp(`<span id="${id}" class="md-app-block__sr-only">Confirm turns on once the first and last rows`))
  assert.match(waiting, /<p class="md-app-block__sr-only" role="status">Example lists 1 item to confirm\.<\/p>/)
  assert.doesNotMatch(view(true), /aria-describedby/, 'an armed Confirm needs no description')
})

test('an incomplete confirmation keeps the conversation rows and says why Confirm is disabled', () => {
  const html = passiveView([{ key: 'send-batch:sel-1', confirming: true, confirmation: [{ title: '', facts: [] }] }])
  assert.deepEqual(rowTitles(html), ['Row A', 'Row B'])
  assert.match(html, /From this conversation/, 'the saved rows keep their caption')
  assert.doesNotMatch(html, /md-app-block__list-edge/)
  assert.match(html, /class="md-app-block__action" disabled="">Confirm/)
  assert.match(html, /Its full current identity is unavailable here/)
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

// Live rows, title, pills and the open-only action.
function rowView(fields, { confirmArmed = true } = {}) {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...base, interaction: 'inline',
    title: '2 changes ready', action: { label: 'Send all', intent: 'send-batch:sel-1' },
    items: [{ title: 'Row A', intent: 'open:a', facts: [{ label: 'Project', value: 'owner/a' }, { label: 'Change', value: '1 file · +2 −0' }] },
      { title: 'Row B', intent: 'open:b' }] }) })
  const state = sessionState({ type: 'moebius:app-block-state', sessionId: 's', actions: [{ key: 'send-batch:sel-1', label: 'Send all' }],
    ...fields }, 's', new Set(['send-batch:sel-1']), new Set(['open:a', 'open:b']))
  return renderToStaticMarkup(createElement(PassiveBlockView, { block, app: admitted[0], state, locked: false, confirmArmed, onEvent() {} }))
}

test('rows read as a title and one metadata line, with labels kept for assistive technology', () => {
  const html = rowView({})
  assert.match(html, /intent=open%3Aa"[^>]*>Row A<\/a><dl class="md-app-block__facts"><div><dt>Project<\/dt><dd>owner\/a<\/dd><\/div><div><dt>Change<\/dt>/)
  assert.doesNotMatch(html, /md-app-block__item-state|md-app-block__pill/, 'no live state until the app reports one')
})

test('the app reports each row\'s status, link and note, and the header says what happened', () => {
  const html = rowView({ title: '1 of 2 sent', items: [
    { intent: 'open:a', status: 'Open', statusTone: 'success', links: [{ label: '#12', url: 'https://github.com/owner/a/pull/12' }] },
    { intent: 'open:b', status: 'Not sent', statusTone: 'danger', note: 'The push was refused.' }],
  actions: [{ key: 'send-batch:sel-1', label: 'Open Example', open: true }] })
  assert.match(html, /<strong>1 of 2 sent<\/strong>/)
  assert.match(html, /aria-label="1 of 2 sent"/)
  assert.match(html, /<p class="md-app-block__sr-only" role="status">1 of 2 sent<\/p>/, 'the new title is announced')
  const [rowA, rowB] = html.split('<li>').slice(1)
  assert.match(rowA, /<span class="md-app-block__item-state"><span class="md-app-block__links"><a class="md-app-block__link" href="https:\/\/github\.com\/owner\/a\/pull\/12" target="_blank" rel="noopener noreferrer" aria-label="#12, Row A, opens in a new tab">#12<svg[^]*<\/a><\/span><span class="md-app-block__pill is-success">Open<\/span><\/span>/,
    'a short PR label names its row for assistive technology')
  assert.match(rowB, /<p class="md-app-block__item-note">The push was refused\.<\/p><\/div><span class="md-app-block__item-state"><span class="md-app-block__pill is-danger">Not sent<\/span>/)
  // The open-only action is a link to the block in the app, never a button.
  assert.match(html, /<footer>[^]*<a class="md-app-block__action is-secondary" href="\/shell\/\?app=example&amp;intent=open%3Aitem-42">Open Example<\/a>/)
  assert.doesNotMatch(html, /<button[^>]*md-app-block__action/)
})

test('a busy row shows a spinner pill; a busy action shows a spinner in its disabled button', () => {
  const html = rowView({ title: 'Sending 2 changes', items: [{ intent: 'open:a', status: 'Sending', busy: true }, { intent: 'open:b', busy: true }],
    actions: [{ key: 'send-batch:sel-1', label: 'Sending…', busy: true, disabled: true }] })
  assert.match(html, /<span class="md-app-block__pill is-neutral"><span class="md-app-block__spinner" aria-hidden="true"><\/span>Sending<\/span>/)
  assert.match(html, /<span class="md-app-block__pill is-neutral"><span class="md-app-block__spinner" aria-hidden="true"><\/span><span class="md-app-block__sr-only">Working<\/span><\/span>/)
  assert.match(html, /<button type="button" class="md-app-block__action" disabled="" aria-busy="true" title="Sending…"><span class="md-app-block__spinner" aria-hidden="true"><\/span>Sending…<\/button>/)
})

test('while the app\'s list is confirming, saved rows and their live state are not shown', () => {
  const html = rowView({ title: '2 changes ready', items: [{ intent: 'open:a', status: 'Open', links: [{ label: '#12', url: 'https://github.com/owner/a/pull/12' }] }],
    actions: [{ key: 'send-batch:sel-1', confirming: true, open: true, confirmation: [{ title: 'Exact A', facts: [{ label: 'Project', value: 'owner/a' }] }] }] })
  assert.deepEqual(rowTitles(html), ['Exact A'])
  assert.doesNotMatch(html, /md-app-block__item-state|#12|is-secondary/)
  assert.match(html, />Not now<\/button><button type="button" class="md-app-block__action">Confirm</)
})

test('a hidden action leaves no empty footer', () => {
  const html = rowView({ title: '2 pull requests opened', actions: [{ key: 'send-batch:sel-1', hidden: true }] })
  assert.doesNotMatch(html, /<footer>/)
  assert.match(html, /<strong>2 pull requests opened<\/strong>/)
})

// Fact labels are visible unless a block opts in, and never hidden on an
// app's confirmation rows: an old app may carry meaning in the label alone.
function directionView(fence, actions) {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...base, interaction: 'inline',
    action: { label: 'Transfer', intent: 'transfer:t1' }, items: [{ title: 'Transfer', intent: 'open:t1',
      facts: [{ label: 'From account', value: 'Alice' }, { label: 'To account', value: 'Bob' }] }], ...fence }) })
  const state = sessionState({ type: 'moebius:app-block-state', sessionId: 's', actions }, 's', new Set(['transfer:t1']), new Set(['open:t1']))
  return renderToStaticMarkup(createElement(PassiveBlockView, { block, app: admitted[0], state, locked: false, confirmArmed: true, onEvent() {} }))
}
const confirmTransfer = (from, to) => [{ key: 'transfer:t1', confirming: true,
  confirmation: [{ title: 'Transfer 10', facts: [{ label: 'From account', value: from }, { label: 'To account', value: to }] }] }]

test('an old app keeps visible fact labels on its rows and confirmation rows', () => {
  const idle = directionView({}, [{ key: 'transfer:t1', label: 'Transfer' }])
  assert.match(idle, /<section class="md-app-block"/, 'no value-only opt-in')
  assert.match(idle, /<dt>From account<\/dt><dd>Alice<\/dd>/)
  const forward = directionView({}, confirmTransfer('Alice', 'Bob'))
  const backward = directionView({}, confirmTransfer('Bob', 'Alice'))
  assert.match(forward, /<li class="is-confirmation">/)
  assert.match(forward, /<dt>From account<\/dt><dd>Alice<\/dd><\/div><div><dt>To account<\/dt><dd>Bob<\/dd>/)
  assert.match(backward, /<dt>From account<\/dt><dd>Bob<\/dd><\/div><div><dt>To account<\/dt><dd>Alice<\/dd>/)
})

test('value-only row facts are an explicit opt-in, from the block or the session, and never cover confirmation rows', () => {
  for (const [fence, extra] of [[{ item_facts: 'values' }, {}], [{}, { itemFacts: 'values' }]]) {
    const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...base, interaction: 'inline',
      action: { label: 'Transfer', intent: 'transfer:t1' }, items: [{ title: 'Transfer', intent: 'open:t1', facts: [{ label: 'Project', value: 'owner/a' }] }], ...fence }) })
    const view = actions => renderToStaticMarkup(createElement(PassiveBlockView, { block, app: admitted[0], locked: false, confirmArmed: true, onEvent() {},
      state: sessionState({ type: 'moebius:app-block-state', sessionId: 's', actions, ...extra }, 's', new Set(['transfer:t1']), new Set(['open:t1'])) }))
    assert.match(view([{ key: 'transfer:t1', label: 'Transfer' }]), /<section class="md-app-block md-app-block--value-facts"/)
    // The opt-in hides labels only on rows that are not confirmation rows (AppBlock.css).
    assert.match(view(confirmTransfer('Alice', 'Bob')), /<li class="is-confirmation">[^]*<dt>From account<\/dt>/)
  }
  for (const value of ['labels', true, 'VALUES']) {
    assert.equal(appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...base, item_facts: value }) }).itemFacts, '')
  }
})

test('value-only confirmation facts need their own opt-in; another app with value-only rows keeps labels at Confirm', () => {
  const view = (fence, actions) => {
    const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...base, interaction: 'inline',
      action: { label: 'Transfer', intent: 'transfer:t1' }, items: [{ title: 'Transfer', intent: 'open:t1', facts: [{ label: 'Project', value: 'owner/a' }] }], ...fence }) })
    return renderToStaticMarkup(createElement(PassiveBlockView, { block, app: admitted[0], locked: false, confirmArmed: true, onEvent() {},
      state: sessionState({ type: 'moebius:app-block-state', sessionId: 's', actions }, 's', new Set(['transfer:t1']), new Set(['open:t1'])) }))
  }
  // A generic (non-Contribute) block: value-only rows, no confirmation opt-in.
  const generic = view({ item_facts: 'values' }, confirmTransfer('Alice', 'Bob'))
  assert.match(generic, /<section class="md-app-block md-app-block--value-facts"/)
  assert.doesNotMatch(generic, /md-app-block--confirm-values/, 'its confirmation keeps visible labels')
  assert.match(generic, /<li class="is-confirmation">[^]*<dt>From account<\/dt><dd>Alice<\/dd>/)
  const opted = view({ item_facts: 'values', confirm_facts: 'values' }, confirmTransfer('Alice', 'Bob'))
  assert.match(opted, /<section class="md-app-block md-app-block--value-facts md-app-block--confirm-values"/)
  assert.match(opted, /<dt>From account<\/dt><dd>Alice<\/dd>/, 'the labels stay in the markup for assistive technology')
  assert.match(view({ confirm_facts: 'values' }, [{ key: 'transfer:t1', label: 'Transfer' }]), /<section class="md-app-block md-app-block--confirm-values"/)
  for (const value of ['labels', true, 'VALUES', null]) {
    assert.equal(appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...base, confirm_facts: value }) }).confirmFacts, '')
  }
})

test('a merged row can use the accent tone, and a live title scopes the caption to the rows', () => {
  const html = rowView({ title: '1 pull request merged', items: [{ intent: 'open:a', status: 'Merged', statusTone: 'accent' }] })
  assert.match(html, /md-app-block__pill is-accent">Merged</)
  assert.match(html, /md-app-block__source">Rows from this conversation</)
  assert.match(rowView({}), /md-app-block__source">From this conversation</)
})

// rows-v2: the app's own rows, a header state mark, and a folded block.
function appRowsView(fields, { onJump = null, rowItems = [{ title: 'Saved row', intent: 'open:a', action: { label: 'Send', intent: 'send:a' },
  facts: [{ label: 'Project', value: 'owner/saved' }] }] } = {}) {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...base, interaction: 'inline',
    title: 'Saved card', facts: [{ label: 'Saved', value: 'root fact' }],
    action: { label: 'Send all', intent: 'send-batch:sel-1' }, items: rowItems }) })
  const state = sessionState({ type: 'moebius:app-block-state', sessionId: 's', actions: [{ key: 'send-batch:sel-1', label: 'Send all' },
    { key: 'send:a', label: 'Send' }], ...fields }, 's', new Set(['send-batch:sel-1', 'send:a']), new Set(['open:a']), 'example')
  return renderToStaticMarkup(createElement(PassiveBlockView, { block, app: admitted[0], state, locked: false, confirmArmed: true, onEvent() {}, onJump }))
}

test('app rows replace the saved rows, facts and item controls, with no conversation caption', () => {
  const html = appRowsView({ title: '2 contributions', rows: [
    { title: 'First change', meta: 'owner/a · +12 −4', intent: 'review:a-1' },
    { title: 'Second change', meta: 'owner/b · +3 −0', pill: 'Merged', tone: 'accent', links: [{ label: '#9', url: 'https://github.com/owner/b/pull/9' }] }] })
  assert.deepEqual(rowTitles(html), ['First change', 'Second change'])
  assert.match(html, /<a class="md-app-block__item-title" href="\/shell\/\?app=example&amp;intent=review%3Aa-1">First change<\/a>/)
  assert.match(html, /<span class="md-app-block__item-title">Second change<\/span>/, 'a row without an intent links nowhere')
  assert.match(html, /<p class="md-app-block__item-meta">owner\/a · \+12 −4<\/p>/)
  assert.match(html, /#9<svg[^]*<\/a><\/span><span class="md-app-block__pill is-accent">Merged<\/span>/)
  assert.doesNotMatch(html, /Saved row|owner\/saved|root fact|md-app-block__source/)
  assert.equal((html.match(/class="md-app-block__action"/g) || []).length, 1, 'only the block action is drawn')
  assert.match(html, /<footer>[^]*>Send all<\/button>/)
})

test('a live title carries a state mark: spinner, check or exclamation mark, none when neutral', () => {
  assert.match(appRowsView({ title: 'Sending 2', titleBusy: true, rows: [] }),
    /<header><span class="md-app-block__title-mark is-neutral" aria-hidden="true"><span class="md-app-block__spinner"><\/span><\/span><strong>Sending 2<\/strong>/)
  assert.match(appRowsView({ title: '2 pull requests opened', titleTone: 'success', rows: [] }),
    /<span class="md-app-block__title-mark is-success" aria-hidden="true"><svg/)
  assert.match(appRowsView({ title: '1 needs a fix', titleTone: 'attention', rows: [] }),
    /<span class="md-app-block__title-mark is-attention" aria-hidden="true"><span class="md-app-block__title-mark-alert">!<\/span>/)
  assert.doesNotMatch(appRowsView({ title: '2 contributions', rows: [] }), /md-app-block__title-mark/)
})

test('no rows and no visible action fold the block to one line, with a jump only when it is not the newest', () => {
  const fields = { title: 'Continued below', rows: [], actions: [{ key: 'send-batch:sel-1', hidden: true }, { key: 'send:a', hidden: true }] }
  const html = appRowsView(fields, { onJump: () => {} })
  assert.match(html, /<section class="md-app-block md-app-block--folded" aria-label="Continued below">/)
  assert.match(html, /<button type="button" class="md-app-block__jump">Jump to latest<svg/)
  assert.doesNotMatch(html, /<footer>|md-app-block__items|Saved row|md-app-block__open/, 'one quiet line: the jump replaces the open link')
  assert.doesNotMatch(appRowsView(fields), /md-app-block__jump/)
  assert.match(appRowsView(fields), /md-app-block__open/, 'the newest folded block keeps its open link')
  assert.doesNotMatch(appRowsView({ ...fields, actions: [{ key: 'send-batch:sel-1', label: 'Send all' }] }), /md-app-block--folded/,
    'a visible action keeps the block open')
})

test('jumping to the newest block moves focus there, without animation for reduced motion', async () => {
  const { jumpTo } = await import('../markdown/PassiveAppBlock.jsx')
  const calls = []
  const node = { attrs: {}, hasAttribute(name) { return name in this.attrs }, setAttribute(name, value) { this.attrs[name] = value },
    scrollIntoView: options => calls.push(['scroll', options.behavior]), focus: options => calls.push(['focus', options.preventScroll]) }
  const previous = globalThis.window
  globalThis.window = { matchMedia: query => ({ matches: query.includes('reduce') }) }
  try { jumpTo(node) } finally { globalThis.window = previous }
  assert.deepEqual(calls, [['scroll', 'auto'], ['focus', true]])
  assert.equal(node.attrs.tabindex, '-1')
  jumpTo(null)
})

// ---- ask-v1: the host's ask replaces the block's controls.
const fixAsk = { refs: [{ id: 'row-1', code: 'upstream_conflict', rev: 'abc1234' }] }
const askView = (status, fields = {}) => {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({ ...base, interaction: 'inline',
    action: { label: 'Send all', intent: 'send-batch:sel-1' }, items: [{ title: 'Saved', intent: 'open:a', action: { label: 'Send', intent: 'send:a' } }] }) })
  const state = sessionState({ type: 'moebius:app-block-state', sessionId: 's', actions: [{ key: 'send-batch:sel-1', label: 'Open Example', open: true },
    { key: 'send:a', label: 'Send' }], rows: [{ title: 'Row', pill: 'Conflicts', tone: 'attention' }], ask: { ...fixAsk, label: 'Show details' }, ...fields },
  's', new Set(['send-batch:sel-1', 'send:a']), new Set(['open:a']), 'example', { ask: true })
  return renderToStaticMarkup(createElement(PassiveBlockView, { block, app: admitted[0], state, locked: false, confirmArmed: true, onEvent() {},
    ask: status ? createElement(AskControl, { status, locked: false, onAsk() {} }) : null }))
}

test('a drawn ask is the block\'s one action, with the host\'s own label; the app\'s controls are not drawn beside it', () => {
  const html = askView('ready')
  assert.equal((html.match(/class="md-app-block__action/g) || []).length, 1)
  assert.match(html, /<footer><span class="md-app-block__controls"><button type="button" class="md-app-block__action is-ask"><svg[^>]*>.*<\/svg>Fix in chat<\/button><span class="md-app-block__sr-only" role="status"><\/span><\/span><\/footer>/)
  assert.doesNotMatch(html, /Open Example|>Send<|Show details/)
  assert.match(html, /is-attention">Conflicts</)
  assert.match(askView(null), /Open Example/, 'without an ask the app action stays')
})

test('after a tap the ask waits, and says why only to assistive technology', () => {
  // Requesting is in flight (busy, no settled class); asked and already are settled.
  const sending = askView('sending')
  assert.match(sending, /is-ask is-requesting" disabled="" aria-busy="true" aria-describedby="[^"]+"><span class="md-app-block__spinner" aria-hidden="true"><\/span>Requesting…/)
  assert.doesNotMatch(sending, /is-settled/)
  assert.match(sending, /<span class="md-app-block__sr-only" role="status"><\/span>/, 'nothing is announced while the request is in flight')
  const asked = askView('asked')
  assert.match(asked, /is-ask is-settled" disabled="" aria-describedby="([^"]+)"><svg[^>]*>.*<\/svg>Requested in chat<\/button><span id="\1" class="md-app-block__sr-only">The request is in this chat\./)
  assert.doesNotMatch(asked, /aria-busy|is-requesting/)
  assert.match(asked, /<span class="md-app-block__sr-only" role="status">The request is in this chat\.<\/span>/, 'the acknowledgement is announced')
  assert.match(askView('already'), /is-settled" disabled=""[^>]*>.*Requested in chat<\/button><span[^>]*>This chat already has this request\.<\/span><span class="md-app-block__sr-only" role="status">This chat already has this request\./)
  const locked = renderToStaticMarkup(createElement(AskControl, { status: 'ready', locked: true, onAsk() {} }))
  assert.match(locked, /is-ask" disabled="">/, 'an owner event awaiting acknowledgement locks it too')
})

test('an ask is drawn only for an admitted app on the attested chat\'s newest block, outside recovery', () => {
  const state = { ask: fixAsk, recoveryPending: false }
  const context = { chatId: 'chat-1', latest: true }
  assert.equal(blockAsk({ state, admitted: true, allowed: true, context }), fixAsk)
  for (const input of [
    { state: null, admitted: true, allowed: true, context },
    { state: { ...state, ask: null }, admitted: true, allowed: true, context },
    { state, admitted: false, allowed: true, context },
    { state, admitted: true, allowed: false, context },
    { state, admitted: true, allowed: true, context: null },
    { state, admitted: true, allowed: true, context: { chatId: 'chat-1', latest: false } },
    { state, admitted: true, allowed: true, context: { chatId: '', latest: true } },
    { state: { ...state, recoveryPending: true }, admitted: true, allowed: true, context },
  ]) assert.equal(blockAsk(input), null, JSON.stringify(input))
})
