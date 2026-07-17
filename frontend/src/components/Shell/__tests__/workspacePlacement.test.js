import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { makeTab, tabKey } from '../tabModel.js'
import * as paneModel from '../paneModel.js'
import {
  ACTIVATE_FOREGROUND,
  ACTIVATE_IN_BACKGROUND,
  PLACE_BESIDE_SOURCE,
  PLACE_WITH_FOCUS,
  PLACE_WITH_SOURCE,
  WORKSPACE_OPEN_ITEM,
  builtAppWorkspaceRequest,
  openItemWorkspaceRequest,
  resolveWorkspaceRequest,
  workspaceRequestFromSystemEvent,
} from '../workspacePlacement.js'

const CHAT = (id) => makeTab('chat', id)
const APP = (id) => makeTab('app', id)

// A two-pane row workspace: p0 | p1. Each pane's last tab is active unless named.
function twoPaneWs(aTabs, bTabs, { focused = 'p0', activeA, activeB } = {}) {
  return paneModel.normalize({
    v: 1,
    layout: { id: 's1', dir: 'row', ratio: 0.5, a: 'p0', b: 'p1' },
    panes: {
      p0: { id: 'p0', tabs: aTabs, activeTabKey: activeA ?? (aTabs.length ? tabKey(aTabs[aTabs.length - 1]) : null) },
      p1: { id: 'p1', tabs: bTabs, activeTabKey: activeB ?? (bTabs.length ? tabKey(bTabs[bTabs.length - 1]) : null) },
    },
    focusedPaneId: focused,
    nextId: 2,
  })
}

function env(ws, { mode = 'wide', rect = { w: 1400, h: 900 }, liveApps = [] } = {}) {
  return { mode, contentRect: rect, projected: paneModel.projectLayout(ws, mode, rect), liveApps }
}

function req(item, source, placement, activation) {
  return { type: WORKSPACE_OPEN_ITEM, item, source, placement, activation, reason: 'test' }
}

const keysOf = (pane) => pane.tabs.map(tabKey)

// ── Request vocabulary (design §6.1) ────────────────────────────────────────

test('built app placement names intent without naming a tab strip or pane', () => {
  assert.deepEqual(builtAppWorkspaceRequest('chat-a', 42), {
    type: WORKSPACE_OPEN_ITEM,
    item: makeTab('app', 42),
    source: makeTab('chat', 'chat-a'),
    placement: PLACE_BESIDE_SOURCE,
    activation: ACTIVATE_IN_BACKGROUND,
    reason: 'chat-built-app',
  })
})

test('invalid app and chat identities cannot produce workspace requests', () => {
  assert.equal(builtAppWorkspaceRequest('', 42), null)
  assert.equal(builtAppWorkspaceRequest(null, 42), null)
  assert.equal(builtAppWorkspaceRequest('chat-a', 0), null)
  assert.equal(builtAppWorkspaceRequest('chat-a', 'not-an-id'), null)
})

// ── open_item event mapping (design §6.3) ───────────────────────────────────

test('app_created and open_item are the live events that request placement', () => {
  assert.deepEqual(
    workspaceRequestFromSystemEvent({ type: 'app_created', appId: '42', chatId: 'chat-a' }),
    builtAppWorkspaceRequest('chat-a', 42),
  )
  assert.equal(workspaceRequestFromSystemEvent({ type: 'app_updated', appId: '42', chatId: 'chat-a' }), null)
  assert.equal(workspaceRequestFromSystemEvent({ type: 'app_created', appId: '42' }), null)

  const opened = workspaceRequestFromSystemEvent({
    type: 'open_item', itemKind: 'app', itemId: '7',
    sourceKind: 'chat', sourceId: 'chat-a',
    placement: 'beside-source', activation: 'background',
  })
  assert.deepEqual(opened, {
    type: WORKSPACE_OPEN_ITEM,
    item: makeTab('app', 7),
    source: makeTab('chat', 'chat-a'),
    placement: PLACE_BESIDE_SOURCE,
    activation: ACTIVATE_IN_BACKGROUND,
    reason: 'agent-open-item',
  })
})

test('open_item maps a chat item and honours foreground with-focus', () => {
  const r = openItemWorkspaceRequest({
    type: 'open_item', itemKind: 'chat', itemId: 'chat-z',
    placement: 'with-focus', activation: 'foreground',
  })
  assert.deepEqual(r, {
    type: WORKSPACE_OPEN_ITEM,
    item: makeTab('chat', 'chat-z'),
    source: null,
    placement: PLACE_WITH_FOCUS,
    activation: ACTIVATE_FOREGROUND,
    reason: 'agent-open-item',
  })
})

test('open_item defaults: background, and beside-source iff a source is present', () => {
  const withSource = openItemWorkspaceRequest({
    itemKind: 'app', itemId: '9', sourceKind: 'chat', sourceId: 'c',
  })
  assert.equal(withSource.placement, PLACE_BESIDE_SOURCE)
  assert.equal(withSource.activation, ACTIVATE_IN_BACKGROUND)

  const noSource = openItemWorkspaceRequest({ itemKind: 'app', itemId: '9' })
  assert.equal(noSource.source, null)
  assert.equal(noSource.placement, PLACE_WITH_FOCUS)
  assert.equal(noSource.activation, ACTIVATE_IN_BACKGROUND)
})

test('open_item drops malformed item ids and unknown kinds (silent no-op upstream)', () => {
  assert.equal(openItemWorkspaceRequest({ itemKind: 'app', itemId: 'not-a-number' }), null)
  assert.equal(openItemWorkspaceRequest({ itemKind: 'widget', itemId: '1' }), null)
  assert.equal(openItemWorkspaceRequest({ itemKind: 'app', itemId: '' }), null)
  // An app source with a non-numeric id is rejected outright.
  assert.equal(openItemWorkspaceRequest({
    itemKind: 'chat', itemId: 'c', sourceKind: 'app', sourceId: 'nope',
  }), null)
})

// ── resolver: forward-compat / malformed request ────────────────────────────

test('resolver ignores an unsupported (future) placement as a strict no-op', () => {
  const ws = paneModel.seedFromFlatTabs([CHAT('a')])
  const bad = req(APP(9), CHAT('a'), 'replace-source', ACTIVATE_IN_BACKGROUND)
  assert.equal(resolveWorkspaceRequest(ws, bad, env(ws)), ws)
})

// ── resolver: beside-source × mode (the §6.2 table) ─────────────────────────

test('beside-source + background · phone: a tab after source, no on-screen switch', () => {
  const ws = paneModel.seedFromFlatTabs([CHAT('a')])
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND),
    env(ws, { mode: 'phone', rect: { w: 400, h: 800 } }),
  )
  assert.equal(paneModel.paneIdsInOrder(out).length, 1, 'no split on a phone')
  assert.deepEqual(keysOf(out.panes.p0), ['chat:a', 'app:9'], 'inserted right after source')
  assert.equal(out.panes.p0.activeTabKey, 'chat:a', 'background never switches the on-screen tab')
})

test('beside-source + foreground · phone: insert and activate', () => {
  const ws = paneModel.seedFromFlatTabs([CHAT('a')])
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_FOREGROUND),
    env(ws, { mode: 'phone', rect: { w: 400, h: 800 } }),
  )
  assert.equal(out.panes.p0.activeTabKey, 'app:9', 'foreground activates the item')
})

test('beside-source + background · tile single pane: split, item active in the NEW pane, focus stays', () => {
  const ws = paneModel.seedFromFlatTabs([CHAT('a')])
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND),
    env(ws, { mode: 'wide', rect: { w: 1400, h: 900 } }),
  )
  const leaves = paneModel.paneIdsInOrder(out)
  assert.equal(leaves.length, 2, 'a new pane bloomed beside the source')
  assert.equal(out.focusedPaneId, 'p0', 'focus and keyboard stay put on a background split')
  const newPane = out.panes[leaves.find(id => id !== 'p0')]
  assert.deepEqual(keysOf(newPane), ['app:9'])
  assert.equal(newPane.activeTabKey, 'app:9', 'the item is active within the new pane')
  assert.deepEqual(keysOf(out.panes.p0), ['chat:a'], 'the source pane is unchanged')
})

test('beside-source + foreground · tile single pane: split AND focus the new pane', () => {
  const ws = paneModel.seedFromFlatTabs([CHAT('a')])
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_FOREGROUND),
    env(ws, { mode: 'wide' }),
  )
  const leaves = paneModel.paneIdsInOrder(out)
  assert.notEqual(out.focusedPaneId, 'p0', 'foreground moves focus to the new pane')
  assert.equal(out.panes[out.focusedPaneId].activeTabKey, 'app:9')
  assert.equal(leaves.length, 2)
})

test('beside-source + background · tile multi-pane: companion pane, inactive tab + no switch', () => {
  // p0 = source chat a; p1 = an app pane whose app 5 has chat_id 'a' (companion).
  const ws = twoPaneWs([CHAT('a')], [APP(5)])
  const liveApps = [{ id: 5, chat_id: 'a' }, { id: 9, chat_id: 'a' }]
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND),
    env(ws, { mode: 'wide', liveApps }),
  )
  assert.equal(paneModel.paneIdsInOrder(out).length, 2, 'no new pane — it joins the companion')
  assert.deepEqual(keysOf(out.panes.p1), ['app:5', 'app:9'], 'the item joins the companion pane')
  assert.equal(out.panes.p1.activeTabKey, 'app:5', 'the companion pane keeps its visible content')
  assert.equal(out.panes.p0.activeTabKey, 'chat:a', 'the source pane keeps its visible content')
  assert.equal(out.focusedPaneId, 'p0', 'background never moves focus')
})

test('beside-source + foreground · tile multi-pane: companion pane, activated + focused', () => {
  const ws = twoPaneWs([CHAT('a')], [APP(5)])
  const liveApps = [{ id: 5, chat_id: 'a' }, { id: 9, chat_id: 'a' }]
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_FOREGROUND),
    env(ws, { mode: 'wide', liveApps }),
  )
  assert.equal(out.panes.p1.activeTabKey, 'app:9', 'foreground activates the companion tab')
  assert.equal(out.focusedPaneId, 'p1', 'and focuses the companion pane')
})

test('beside-source + background · tile multi-pane, no companion: split the source pane', () => {
  const ws = twoPaneWs([CHAT('a')], [CHAT('b')])
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND),
    env(ws, { mode: 'wide', liveApps: [{ id: 9, chat_id: 'a' }] }),
  )
  const leaves = paneModel.paneIdsInOrder(out)
  assert.equal(leaves.length, 3, 'the source pane split — no companion existed')
  const newId = leaves.find(id => id !== 'p0' && id !== 'p1')
  assert.deepEqual(keysOf(out.panes[newId]), ['app:9'])
  assert.equal(out.focusedPaneId, 'p0', 'background split keeps focus')
})

test('beside-source · tile: an infeasible split degrades to a background tab beside source', () => {
  // Force infeasibility: a "wide" mode request against a rect too small for two
  // minimums (canSplit refuses both axes), so the ladder degrades to a tab.
  const ws = paneModel.seedFromFlatTabs([CHAT('a')])
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND),
    env(ws, { mode: 'wide', rect: { w: 100, h: 100 } }),
  )
  assert.equal(paneModel.paneIdsInOrder(out).length, 1, 'no split when neither axis is feasible')
  assert.deepEqual(keysOf(out.panes.p0), ['chat:a', 'app:9'], 'degraded to a tab beside source')
  assert.equal(out.panes.p0.activeTabKey, 'chat:a')
})

// ── resolver: with-source / with-focus ──────────────────────────────────────

test('with-source: a tab in the source pane; activate iff foreground', () => {
  const wsBg = twoPaneWs([CHAT('a')], [CHAT('b')], { focused: 'p1' })
  const bg = resolveWorkspaceRequest(
    wsBg, req(APP(9), CHAT('a'), PLACE_WITH_SOURCE, ACTIVATE_IN_BACKGROUND),
    env(wsBg, { mode: 'wide' }),
  )
  assert.deepEqual(keysOf(bg.panes.p0), ['chat:a', 'app:9'], 'lands in the source pane')
  assert.equal(bg.panes.p0.activeTabKey, 'chat:a', 'background does not activate')
  assert.equal(bg.focusedPaneId, 'p1', 'background does not move focus')

  const fg = resolveWorkspaceRequest(
    wsBg, req(APP(9), CHAT('a'), PLACE_WITH_SOURCE, ACTIVATE_FOREGROUND),
    env(wsBg, { mode: 'wide' }),
  )
  assert.equal(fg.panes.p0.activeTabKey, 'app:9', 'foreground activates the source-pane tab')
})

test('with-focus: append to the focused pane, ignoring the source', () => {
  const ws = twoPaneWs([CHAT('a')], [CHAT('b')], { focused: 'p1' })
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_WITH_FOCUS, ACTIVATE_IN_BACKGROUND),
    env(ws, { mode: 'wide' }),
  )
  assert.deepEqual(keysOf(out.panes.p1), ['chat:b', 'app:9'], 'appended to the FOCUSED pane, not the source')
  assert.deepEqual(keysOf(out.panes.p0), ['chat:a'])
})

// ── resolver: already-open + source-missing ─────────────────────────────────

test('item already open: background is a strict no-op; foreground focuses its pane/tab', () => {
  const ws = twoPaneWs([CHAT('a'), APP(5)], [CHAT('b')], { focused: 'p1', activeA: 'chat:a' })
  const bg = resolveWorkspaceRequest(
    ws, req(APP(5), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND),
    env(ws, { mode: 'wide' }),
  )
  assert.equal(bg, ws, 'already-open background is the same reference (no-op)')

  const fg = resolveWorkspaceRequest(
    ws, req(APP(5), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_FOREGROUND),
    env(ws, { mode: 'wide' }),
  )
  assert.equal(fg.focusedPaneId, 'p0', 'foreground focuses the pane already holding the item')
  assert.equal(fg.panes.p0.activeTabKey, 'app:5', 'and activates the item tab')
})

test('source missing / not open: degrade to with-focus (append to focused pane)', () => {
  const ws = twoPaneWs([CHAT('a')], [CHAT('b')], { focused: 'p1' })
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('ghost'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND),
    env(ws, { mode: 'wide' }),
  )
  assert.deepEqual(keysOf(out.panes.p1), ['chat:b', 'app:9'], 'degraded to the focused pane')
  assert.equal(paneModel.paneIdsInOrder(out).length, 2, 'no split on a missing source')
})

// ── resolver: protected eviction at MAX_PANE_TABS (design §3.6) ──────────────

test('pane at MAX_PANE_TABS: protected eviction spares source, item, active, visible', () => {
  const full = [CHAT('a'), CHAT('b'), CHAT('c'), CHAT('d'), CHAT('e'), APP(7)]
  const ws = paneModel.normalize({
    v: 1, layout: 'p0',
    panes: { p0: { id: 'p0', tabs: full, activeTabKey: 'app:7' } },
    focusedPaneId: 'p0', nextId: 1,
  })
  assert.equal(ws.panes.p0.tabs.length, paneModel.MAX_PANE_TABS)
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND),
    env(ws, { mode: 'phone', rect: { w: 400, h: 800 } }),
  )
  const keys = keysOf(out.panes.p0)
  assert.equal(keys.length, paneModel.MAX_PANE_TABS, 'the per-pane cap still holds')
  assert.ok(keys.includes('app:9'), 'the item was admitted')
  assert.ok(keys.includes('chat:a'), 'the source was protected from eviction')
  assert.ok(keys.includes('app:7'), 'the active/visible tab was protected from eviction')
  assert.ok(!keys.includes('chat:b'), 'the oldest UNPROTECTED tab was evicted')
  assert.equal(out.panes.p0.activeTabKey, 'app:7', 'the on-screen tab is unchanged')
})

// ── batched producer order (built-apps arrival) ─────────────────────────────

test('two built apps for one chat keep producer order beside the source (phone)', () => {
  // Mirrors placeInWorkspace folding two requests in reverse: A then B → A, B.
  let ws = paneModel.seedFromFlatTabs([CHAT('a')])
  const requests = [builtAppWorkspaceRequest('a', 41), builtAppWorkspaceRequest('a', 42)]
  for (let i = requests.length - 1; i >= 0; i -= 1) {
    ws = resolveWorkspaceRequest(ws, requests[i], env(ws, { mode: 'phone', rect: { w: 400, h: 800 } }))
  }
  assert.deepEqual(keysOf(ws.panes.p0), ['chat:a', 'app:41', 'app:42'])
})

// ── the durable-list reconnect wiring is still in place ─────────────────────

test('shell reconciles the durable app list whenever the system stream reconnects', () => {
  const shellSource = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
  assert.match(
    shellSource,
    /useSystemEventStream\(handleSystemEvent, \{ onOpen: refreshApps \}\)/,
  )
})
