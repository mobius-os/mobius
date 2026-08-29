import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { makeTab, tabKey } from '../tabModel.js'
import * as paneModel from '../paneModel.js'
import { effectiveViewMode, initialModeState, modeReducer } from '../modeMachine.js'
import {
  ACTIVATE_FOREGROUND,
  ACTIVATE_IN_BACKGROUND,
  ACTIVATE_LIVE_PREVIEW,
  PLACE_BESIDE_SOURCE,
  PLACE_WITH_FOCUS,
  PLACE_WITH_SOURCE,
  WORKSPACE_OPEN_ITEM,
  attentionForRequest,
  builtAppWorkspaceRequest,
  openItemWorkspaceRequest,
  resolveWorkspaceRequest,
  resolveWorkspaceRequests,
  workspaceRequestFromSystemEvent,
} from '../workspacePlacement.js'

const CHAT = (id) => makeTab('chat', id)
const APP = (id) => makeTab('app', id)

function builderSeed(tabs) {
  return paneModel.setViewMode(paneModel.seedFromFlatTabs(tabs), 'panes')
}

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

// The on-screen content in a given mode: the active tab of every leaf the
// projection shows. The projection-level background guarantee is that this set
// never LOSES a member across a background placement (it may gain a new pane).
function visibleActiveKeys(ws, mode, rect) {
  const proj = paneModel.projectLayout(ws, mode, rect)
  return new Set(proj.visibleLeaves.map((id) => ws.panes[id]?.activeTabKey).filter(Boolean))
}

// Assert a BACKGROUND placement dropped nothing from the screen (design §6.2):
// visible-before ⊆ visible-after in the given mode.
function assertNoVisibleContentVanished(before, after, mode, rect) {
  const va = visibleActiveKeys(after, mode, rect)
  for (const key of visibleActiveKeys(before, mode, rect)) {
    assert.ok(va.has(key), `background placement hid on-screen content ${key} (${mode})`)
  }
}

// ── Request vocabulary (design §6.1) ────────────────────────────────────────

test('built app placement names intent without naming a tab strip or pane', () => {
  assert.deepEqual(builtAppWorkspaceRequest('chat-a', 42), {
    type: WORKSPACE_OPEN_ITEM,
    item: makeTab('app', 42),
    source: makeTab('chat', 'chat-a'),
    placement: PLACE_BESIDE_SOURCE,
    activation: ACTIVATE_LIVE_PREVIEW,
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

test('app_preview_ready and open_item are the live events that request placement', () => {
  assert.deepEqual(
    workspaceRequestFromSystemEvent({ type: 'app_preview_ready', appId: '42', chatId: 'chat-a' }),
    builtAppWorkspaceRequest('chat-a', 42),
  )
  assert.equal(workspaceRequestFromSystemEvent({ type: 'app_updated', appId: '42', chatId: 'chat-a' }), null)
  assert.equal(workspaceRequestFromSystemEvent({ type: 'app_created', appId: '42', chatId: 'chat-a' }), null)
  assert.equal(workspaceRequestFromSystemEvent({ type: 'app_preview_ready', appId: '42' }), null)

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

test('open_item defaults an OMITTED placement/activation but no-ops an UNKNOWN one', () => {
  const withSource = openItemWorkspaceRequest({
    itemKind: 'app', itemId: '9', sourceKind: 'chat', sourceId: 'c',
  })
  assert.equal(withSource.placement, PLACE_BESIDE_SOURCE)
  assert.equal(withSource.activation, ACTIVATE_IN_BACKGROUND)

  const noSource = openItemWorkspaceRequest({ itemKind: 'app', itemId: '9' })
  assert.equal(noSource.source, null)
  assert.equal(noSource.placement, PLACE_WITH_FOCUS)
  assert.equal(noSource.activation, ACTIVATE_IN_BACKGROUND)

  // Forward-compat: a PRESENT-but-unrecognized value is a silent no-op (a cached
  // older shell must skip a v2 value, never coerce it to a wrong default).
  assert.equal(openItemWorkspaceRequest({
    itemKind: 'app', itemId: '9', placement: 'open-in-window',
  }), null)
  assert.equal(openItemWorkspaceRequest({
    itemKind: 'app', itemId: '9', activation: 'urgent',
  }), null)
})

test('open_item drops malformed item ids but OMITS a malformed source (degrade)', () => {
  assert.equal(openItemWorkspaceRequest({ itemKind: 'app', itemId: 'not-a-number' }), null)
  assert.equal(openItemWorkspaceRequest({ itemKind: 'widget', itemId: '1' }), null)
  assert.equal(openItemWorkspaceRequest({ itemKind: 'app', itemId: '' }), null)
  // A malformed source (non-numeric app id) is OMITTED, not fatal: the request
  // survives with source:null and the resolver degrades it to with-focus.
  const r = openItemWorkspaceRequest({
    itemKind: 'chat', itemId: 'c', sourceKind: 'app', sourceId: 'nope',
  })
  assert.ok(r, 'the request is not dropped')
  assert.equal(r.source, null, 'the malformed source is omitted')
  assert.equal(r.placement, PLACE_WITH_FOCUS, 'no source → default with-focus')
})

// Agents place chats and apps only; Settings is an owner-driven surface, never
// an agent-requestable item. The itemKind whitelist (chat|app) is the structural
// exclusion — a 'settings' open-item is dropped exactly like any unknown kind,
// so no resolver path, source, or activation can smuggle a Settings tab into a
// pane on the agent's behalf. (The backend open_item enum excludes it too.)
test('an agent open_item can NEVER request the Settings tab', () => {
  assert.equal(openItemWorkspaceRequest({ itemKind: 'settings', itemId: 'settings' }), null)
  assert.equal(openItemWorkspaceRequest({
    itemKind: 'settings', itemId: 'settings', activation: 'foreground',
  }), null)
  // Even paired with a valid relational source it is dropped, not degraded.
  assert.equal(openItemWorkspaceRequest({
    itemKind: 'settings', itemId: 'settings', sourceKind: 'chat', sourceId: 'c',
    placement: PLACE_BESIDE_SOURCE,
  }), null)
  // The system-event bridge is the other agent entry point — it too yields nothing.
  assert.equal(
    workspaceRequestFromSystemEvent({ type: 'open_item', itemKind: 'settings', itemId: 'settings' }),
    null,
  )
})

// ── background attention target (design §6.2 dot) ───────────────────────────

test('attentionForRequest flags a background open by kind, and nothing for foreground', () => {
  const bgApp = openItemWorkspaceRequest({ itemKind: 'app', itemId: '42' })
  assert.deepEqual(attentionForRequest(bgApp), { kind: 'app', id: 42 })

  const bgChat = openItemWorkspaceRequest({ itemKind: 'chat', itemId: 'chat-z' })
  assert.deepEqual(attentionForRequest(bgChat), { kind: 'chat', id: 'chat-z' })

  const fgApp = openItemWorkspaceRequest({
    itemKind: 'app', itemId: '42', activation: 'foreground',
  })
  assert.equal(attentionForRequest(fgApp), null, 'a foreground open is on screen — no dot')

  assert.equal(attentionForRequest(null), null)
  assert.equal(attentionForRequest(builtAppWorkspaceRequest('a', 41)), null,
    'a visible live preview needs no background-attention dot')
})

// ── resolver: forward-compat / malformed request ────────────────────────────

test('resolver ignores an unsupported (future) placement as a strict no-op', () => {
  const ws = paneModel.seedFromFlatTabs([CHAT('a')])
  const bad = req(APP(9), CHAT('a'), 'replace-source', ACTIVATE_IN_BACKGROUND)
  assert.equal(resolveWorkspaceRequest(ws, bad, env(ws)), ws)
})

// ── resolver: beside-source × mode (the §6.2 table) ─────────────────────────

test('beside-source + background · phone: a tab after source, no on-screen switch', () => {
  const ws = builderSeed([CHAT('a')])
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND),
    env(ws, { mode: 'phone', rect: { w: 400, h: 800 } }),
  )
  assert.equal(paneModel.paneIdsInOrder(out).length, 1, 'no split on a phone')
  assert.deepEqual(keysOf(out.panes.p0), ['chat:a', 'app:9'], 'inserted right after source')
  assert.equal(out.panes.p0.activeTabKey, 'chat:a', 'background never switches the on-screen tab')
})

test('beside-source + foreground · phone: insert and activate', () => {
  const ws = builderSeed([CHAT('a')])
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_FOREGROUND),
    env(ws, { mode: 'phone', rect: { w: 400, h: 800 } }),
  )
  assert.equal(out.panes.p0.activeTabKey, 'app:9', 'foreground activates the item')
})

test('live preview · phone: opens a companion pane without replacing its source chat', () => {
  const ws = {
    ...paneModel.setViewMode(builderSeed([CHAT('a')]), 'single'),
    singleScreen: { kind: 'chat', id: 'a' },
  }
  const out = resolveWorkspaceRequest(
    ws,
    builtAppWorkspaceRequest('a', 9),
    env(ws, { mode: 'phone', rect: { w: 400, h: 800 } }),
  )
  assert.equal(out.viewMode, 'panes', 'the live preview reveals the Builder world')
  assert.equal(paneModel.paneIdsInOrder(out).length, 2,
    'the app gets its own phone pane instead of joining the chat pane')
  assert.equal(out.panes[out.focusedPaneId].activeTabKey, 'chat:a',
    'the source chat keeps keyboard focus')
  const appPane = paneModel.paneOf(out, 'app:9')
  assert.ok(appPane)
  assert.notEqual(appPane.id, out.focusedPaneId)
  assert.equal(appPane.activeTabKey, 'app:9', 'the preview is active in its own pane')
})

test('live preview · phone update: moves a parked app out before revealing it', () => {
  const ws = paneModel.setActiveTab(builderSeed([CHAT('a'), APP(9)]), 'p0', 'chat:a')
  const out = resolveWorkspaceRequest(
    ws,
    builtAppWorkspaceRequest('a', 9),
    env(ws, { mode: 'phone', rect: { w: 400, h: 800 } }),
  )
  assert.equal(paneModel.paneIdsInOrder(out).length, 2)
  assert.equal(out.panes[out.focusedPaneId].activeTabKey, 'chat:a',
    'later coherent updates do not interrupt the focused chat')
  const appPane = paneModel.paneOf(out, 'app:9')
  assert.notEqual(appPane.id, out.focusedPaneId)
  assert.equal(appPane.activeTabKey, 'app:9')

  const parked = resolveWorkspaceRequest(
    ws,
    builtAppWorkspaceRequest('a', 9),
    env(ws, { mode: 'phone', rect: { w: 400, h: 350 } }),
  )
  assert.equal(parked.panes.p0.activeTabKey, 'chat:a',
    'without room for a companion, the app stays parked behind the chat')
  assert.equal(parked.focusedPaneId, 'p0')

  const appInUse = builderSeed([CHAT('a'), APP(9)])
  const unchanged = resolveWorkspaceRequest(
    appInUse,
    builtAppWorkspaceRequest('a', 9),
    env(appInUse, { mode: 'phone', rect: { w: 400, h: 800 } }),
  )
  assert.equal(unchanged, appInUse,
    'an app already in use stays in its focused pane while its frame updates')
})

test('live preview · Standard source: preserves chat and Back ownership when Builder focus was elsewhere', () => {
  const ws = {
    ...twoPaneWs([CHAT('a')], [APP(5)], { focused: 'p1' }),
    viewMode: 'single',
    singleScreen: { kind: 'chat', id: 'a' },
  }
  const out = resolveWorkspaceRequest(
    ws,
    builtAppWorkspaceRequest('a', 9),
    env(ws, {
      mode: 'wide',
      rect: { w: 1400, h: 900 },
      liveApps: [{ id: 5, chat_id: 'a' }, { id: 9, chat_id: 'a' }],
    }),
  )
  assert.equal(out.viewMode, 'panes')
  assert.equal(out.focusedPaneId, 'p0',
    'Builder focus follows the same chat Standard was showing, never the preview')
  assert.equal(out.panes.p0.activeTabKey, 'chat:a')
  assert.deepEqual(paneModel.activeContentRoute(out), {
    view: 'chat', chatId: 'a', appId: null, paneId: 'p0',
  }, 'keyboard and Back still belong to the building chat')
  assert.equal(out.panes.p1.activeTabKey, 'app:9',
    'the unfocused companion may switch to the new preview')
})

test('live preview · Standard elsewhere: parks without changing the visible surface', () => {
  const ws = {
    ...paneModel.setViewMode(builderSeed([CHAT('a')]), 'single'),
    singleScreen: { kind: 'chat', id: 'elsewhere' },
  }
  const out = resolveWorkspaceRequest(
    ws,
    builtAppWorkspaceRequest('a', 9),
    env(ws, { mode: 'wide', rect: { w: 1400, h: 900 } }),
  )
  assert.equal(out.viewMode, 'single', 'background work does not leave Standard')
  assert.deepEqual(out.singleScreen, { kind: 'chat', id: 'elsewhere' },
    'the owner keeps the surface they were using')
  assert.ok(paneModel.paneOf(out, 'app:9'), 'the preview is parked in Builder')

  const presented = modeReducer(initialModeState('single'), {
    type: 'sync-committed',
    committedMode: out.viewMode,
  })
  assert.equal(effectiveViewMode(presented), 'single',
    'presentation follows the resolver actual result, never preview intent')
  const afterDrawerSelection = paneModel.setSingleScreen(out, { kind: 'chat', id: 'next' })
  assert.deepEqual(paneModel.activeContentRoute(afterDrawerSelection), {
    view: 'chat', chatId: 'next', appId: null, paneId: afterDrawerSelection.focusedPaneId,
  }, 'a later drawer selection paints the new Standard chat while Builder stays parked')
})

test('beside-source + background · tile single pane: split, item active in the NEW pane, focus stays', () => {
  const ws = builderSeed([CHAT('a')])
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

test('live preview · wide: enters Builder and blooms beside the focused chat', () => {
  const ws = {
    ...paneModel.setViewMode(builderSeed([CHAT('a')]), 'single'),
    singleScreen: { kind: 'chat', id: 'a' },
  }
  const out = resolveWorkspaceRequest(
    ws,
    builtAppWorkspaceRequest('a', 9),
    env(ws, { mode: 'wide', rect: { w: 1400, h: 900 } }),
  )
  assert.equal(out.viewMode, 'panes')
  assert.equal(paneModel.paneIdsInOrder(out).length, 2)
  assert.equal(out.panes[out.focusedPaneId].activeTabKey, 'chat:a',
    'the building chat keeps keyboard focus')
  const appPane = paneModel.paneOf(out, 'app:9')
  assert.ok(appPane)
  assert.equal(appPane.activeTabKey, 'app:9', 'the preview is visible in its pane')
})

test('live preview · compact fallback: parks in the source pane without replacing it', () => {
  const ws = twoPaneWs([CHAT('a')], [CHAT('b')])
  const rect = { w: 800, h: 560 }
  const out = resolveWorkspaceRequest(
    ws,
    builtAppWorkspaceRequest('a', 9),
    env(ws, { mode: 'compact', rect, liveApps: [{ id: 9, chat_id: 'a' }] }),
  )
  assert.equal(paneModel.paneIdsInOrder(out).length, 2,
    'a projection-unsafe third pane is not created')
  assert.deepEqual(keysOf(out.panes.p0), ['chat:a', 'app:9'])
  assert.equal(out.panes.p0.activeTabKey, 'chat:a',
    'the preview cannot take over the shared pane')
  assertNoVisibleContentVanished(ws, out, 'compact', rect)
})

test('live preview · occupied companion: switches that pane without taking chat focus', () => {
  const ws = twoPaneWs([CHAT('a')], [APP(5)])
  const liveApps = [{ id: 5, chat_id: 'a' }, { id: 9, chat_id: 'a' }]
  const out = resolveWorkspaceRequest(
    ws,
    builtAppWorkspaceRequest('a', 9),
    env(ws, { mode: 'wide', liveApps }),
  )
  assert.deepEqual(keysOf(out.panes.p1), ['app:5', 'app:9'])
  assert.equal(out.panes.p1.activeTabKey, 'app:9',
    'the preview switches the companion pane')
  assert.equal(out.focusedPaneId, 'p0')

  const alreadyOpen = twoPaneWs([CHAT('a')], [APP(5), APP(9)], { activeB: 'app:5' })
  const switched = resolveWorkspaceRequest(
    alreadyOpen,
    builtAppWorkspaceRequest('a', 9),
    env(alreadyOpen, {
      mode: 'wide',
      liveApps: [{ id: 5, chat_id: 'a' }, { id: 9, chat_id: 'a' }],
    }),
  )
  assert.equal(switched.panes.p1.activeTabKey, 'app:9',
    'an already-open app switches inside the companion too')
  assert.equal(switched.focusedPaneId, 'p0')
})

test('live preview · focused related panes are excluded from companion placement', () => {
  const ws = twoPaneWs([CHAT('a')], [APP(5)], { focused: 'p1' })
  const out = resolveWorkspaceRequest(
    ws,
    builtAppWorkspaceRequest('a', 9),
    env(ws, {
      mode: 'wide',
      rect: { w: 1400, h: 900 },
      liveApps: [{ id: 5, chat_id: 'a' }, { id: 9, chat_id: 'a' }],
    }),
  )
  assert.equal(out.focusedPaneId, 'p1', 'the app already in use keeps keyboard and Back ownership')
  assert.equal(out.panes.p1.activeTabKey, 'app:5', 'the focused related app is not replaced')
  const previewPane = paneModel.paneOf(out, 'app:9')
  assert.ok(previewPane)
  assert.notEqual(previewPane.id, out.focusedPaneId)
  assert.equal(previewPane.activeTabKey, 'app:9', 'the preview continues down the split ladder')
  assert.deepEqual(paneModel.activeContentRoute(out), {
    view: 'canvas', chatId: null, appId: 5, paneId: 'p1',
  })

  const parked = twoPaneWs(
    [CHAT('a'), APP(4)],
    [APP(5)],
    { focused: 'p0', activeA: 'chat:a' },
  )
  const liveApps = [4, 5, 9].map(id => ({ id, chat_id: 'a' }))
  const reused = resolveWorkspaceRequest(
    parked,
    builtAppWorkspaceRequest('a', 9),
    env(parked, { mode: 'wide', rect: { w: 1400, h: 900 }, liveApps }),
  )
  assert.equal(paneModel.paneIdsInOrder(reused).length, 2, 'no redundant third pane is created')
  assert.equal(reused.focusedPaneId, 'p0')
  assert.equal(reused.panes.p0.activeTabKey, 'chat:a')
  assert.deepEqual(keysOf(reused.panes.p1), ['app:5', 'app:9'])
  assert.equal(reused.panes.p1.activeTabKey, 'app:9')
})

test('beside-source + foreground · tile single pane: split AND focus the new pane', () => {
  const ws = builderSeed([CHAT('a')])
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
  // The new pane is ADDED; both previously-visible panes remain on screen (wide).
  assertNoVisibleContentVanished(ws, out, 'wide', { w: 1400, h: 900 })
})

test('beside-source + background · COMPACT multi-pane: a split would hide the sibling → degrade to tab', () => {
  // The blocker: compact shows only the focused pane + its sibling. Splitting the
  // focused pane to add a 3rd leaf makes the projection drop the OTHER visible
  // pane. A background placement must not do that — degrade to a tab beside source.
  const ws = twoPaneWs([CHAT('a')], [CHAT('b')])
  const rect = { w: 800, h: 560 } // compact (≥700×520, <960×600)
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND),
    env(ws, { mode: 'compact', rect }),
  )
  assert.equal(paneModel.paneIdsInOrder(out).length, 2, 'no split — it would hide the sibling')
  assert.deepEqual(keysOf(out.panes.p0), ['chat:a', 'app:9'], 'degraded to a background tab beside source')
  assert.equal(out.panes.p0.activeTabKey, 'chat:a', 'the source pane keeps its on-screen tab')
  assertNoVisibleContentVanished(ws, out, 'compact', rect)
})

test('beside-source + FOREGROUND compact multi-pane still splits (the projection change is intended)', () => {
  const ws = twoPaneWs([CHAT('a')], [CHAT('b')])
  const rect = { w: 800, h: 560 }
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_FOREGROUND),
    env(ws, { mode: 'compact', rect }),
  )
  assert.equal(paneModel.paneIdsInOrder(out).length, 3, 'foreground split is allowed')
  assert.equal(out.panes[out.focusedPaneId].activeTabKey, 'app:9', 'and focus follows the new pane')
})

test('the projection-level background guarantee holds for EVERY background cell', () => {
  // For every background placement path, no currently-visible pane loses its
  // on-screen content (a new pane may appear; nothing may vanish). The stronger
  // form of §6.2 that would have caught the compact-split blocker.
  const cases = [
    // [label, ws, mode, rect, liveApps]
    ['phone tab-insert', paneModel.seedFromFlatTabs([CHAT('a')]), 'phone', { w: 400, h: 800 }, []],
    ['single-pane wide split', paneModel.seedFromFlatTabs([CHAT('a')]), 'wide', { w: 1400, h: 900 }, []],
    ['single-pane compact split', paneModel.seedFromFlatTabs([CHAT('a')]), 'compact', { w: 800, h: 560 }, []],
    ['wide multi-pane split', twoPaneWs([CHAT('a')], [CHAT('b')]), 'wide', { w: 1400, h: 900 }, [{ id: 9, chat_id: 'a' }]],
    ['compact multi-pane (degrades)', twoPaneWs([CHAT('a')], [CHAT('b')]), 'compact', { w: 800, h: 560 }, [{ id: 9, chat_id: 'a' }]],
    ['companion join', twoPaneWs([CHAT('a')], [APP(5)]), 'wide', { w: 1400, h: 900 }, [{ id: 5, chat_id: 'a' }, { id: 9, chat_id: 'a' }]],
  ]
  for (const [label, ws, mode, rect, liveApps] of cases) {
    const out = resolveWorkspaceRequest(
      ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND),
      env(ws, { mode, rect, liveApps }),
    )
    assertNoVisibleContentVanished(ws, out, mode, rect)
    assert.ok(out !== ws || label === 'no-op', `${label}: produced a placement`)
  }
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

test('live preview · wide update: an app parked with its chat moves into a visible companion pane', () => {
  const ws = {
    ...paneModel.setViewMode(
      paneModel.seedFromFlatTabs([CHAT('a'), APP(5)]),
      'single',
    ),
    singleScreen: { kind: 'chat', id: 'a' },
  }
  const out = resolveWorkspaceRequest(
    ws,
    builtAppWorkspaceRequest('a', 5),
    env(ws, { mode: 'wide', rect: { w: 1400, h: 900 } }),
  )
  assert.equal(out.viewMode, 'panes')
  assert.equal(paneModel.paneIdsInOrder(out).length, 2)
  assert.equal(out.panes[out.focusedPaneId].activeTabKey, 'chat:a')
  assert.equal(paneModel.paneOf(out, 'app:5').activeTabKey, 'app:5')
  assert.notEqual(paneModel.paneOf(out, 'app:5').id, out.focusedPaneId)
})

test('live preview · missing builder source: restores the requesting chat before placing the app', () => {
  const ws = {
    ...paneModel.seedFromFlatTabs([CHAT('old')]),
    viewMode: 'single',
    singleScreen: { kind: 'chat', id: 'new' },
  }
  const out = resolveWorkspaceRequest(
    ws,
    builtAppWorkspaceRequest('new', 9),
    env(ws, { mode: 'wide', rect: { w: 1400, h: 900 } }),
  )
  assert.equal(out.viewMode, 'panes')
  assert.equal(out.panes[out.focusedPaneId].activeTabKey, 'chat:new')
  assert.ok(paneModel.paneOf(out, 'app:9'))
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

// ── resolver: explicit tab ownership ─────────────────────────────────────────

test('placement adds beyond six tabs without evicting existing work', () => {
  const existing = [
    CHAT('a'), CHAT('b'), CHAT('c'), CHAT('d'), CHAT('e'), CHAT('f'), CHAT('g'), APP(7),
  ]
  const ws = paneModel.normalize({
    v: 1, layout: 'p0',
    panes: { p0: { id: 'p0', tabs: existing, activeTabKey: 'app:7' } },
    focusedPaneId: 'p0', nextId: 1,
  })
  const out = resolveWorkspaceRequest(
    ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND),
    env(ws, { mode: 'phone', rect: { w: 400, h: 800 } }),
  )
  const keys = keysOf(out.panes.p0)
  assert.equal(keys.length, existing.length + 1)
  for (const tab of existing) assert.ok(keys.includes(tabKey(tab)))
  assert.equal(keys[1], 'app:9', 'the new item still lands beside its source')
  assert.equal(out.panes.p0.activeTabKey, 'app:7', 'the on-screen tab is unchanged')
})

// ── batch fold == sequential (the real placeInWorkspace fold) ───────────────

// The environment placeInWorkspace passes to resolveWorkspaceRequests.
const batchEnv = (mode, rect, liveApps = []) => ({ mode, contentRect: rect, liveApps })

// Deliver the same requests one dispatch at a time (each its own resolve) — what
// the live app_preview_ready path does.
function applySequential(ws, requests, e) {
  let next = ws
  for (const r of requests) next = resolveWorkspaceRequests(next, [r], e)
  return next
}

test('a batch of built-app placements folds identically to sequential delivery (phone)', () => {
  const seed = paneModel.seedFromFlatTabs([CHAT('a')])
  const requests = [builtAppWorkspaceRequest('a', 41), builtAppWorkspaceRequest('a', 42)]
  const e = batchEnv('phone', { w: 400, h: 800 }, [{ id: 41, chat_id: 'a' }, { id: 42, chat_id: 'a' }])

  const batched = resolveWorkspaceRequests(seed, requests, e)
  const sequential = applySequential(seed, requests, e)
  assert.deepEqual(paneModel.flatten(batched), paneModel.flatten(sequential), 'batch == sequential (phone)')
})

test('a batch of built-app placements folds identically to sequential delivery (wide)', () => {
  // Wide: the first app auto-splits, the second companion-joins that app's pane.
  const seed = paneModel.seedFromFlatTabs([CHAT('a')])
  const requests = [builtAppWorkspaceRequest('a', 41), builtAppWorkspaceRequest('a', 42)]
  const e = batchEnv('wide', { w: 1400, h: 900 }, [{ id: 41, chat_id: 'a' }, { id: 42, chat_id: 'a' }])

  const batched = resolveWorkspaceRequests(seed, requests, e)
  const sequential = applySequential(seed, requests, e)
  assert.deepEqual(paneModel.flatten(batched), paneModel.flatten(sequential), 'batch == sequential (wide)')
  // And build order is preserved in the companion pane: chat | [41, 42].
  const appPane = paneModel.paneIdsInOrder(batched)
    .map(id => batched.panes[id]).find(p => p.tabs.some(t => t.kind === 'app'))
  assert.deepEqual(keysOf(appPane), ['app:41', 'app:42'])
})

// ── Two-worlds: single-mode agent placement (finding F4) ────────────────────
//
// In the SINGLE world the only painted surface is the slot. A FOREGROUND agent
// open must set the slot (so the user SEES it), never mutate the hidden pane tree
// where it would be invisible; BACKGROUND work still parks in the builder tree.

test('single mode + FOREGROUND: sets the slot, leaves the pane tree untouched', () => {
  const ws = { ...twoPaneWs([CHAT('a')], [APP(42)]), viewMode: 'single' }
  const out = resolveWorkspaceRequest(ws, req(APP(9), null, PLACE_WITH_FOCUS, ACTIVATE_FOREGROUND), env(ws))
  assert.deepEqual(out.singleScreen, { kind: 'app', id: '9' }, 'the foregrounded item becomes the slot')
  assert.equal(out.panes, ws.panes, 'the builder pane tree is byte-identical (no OPEN_TAB)')
  assert.equal(paneModel.paneOf(out, 'app:9'), null, 'the item is NOT added to the tree')
})

test('single mode + FOREGROUND for a tree item still sets the slot (no hidden focus)', () => {
  // App 42 is already open in the builder tree. A single-mode foreground open must
  // set the SLOT, not focus the hidden tree pane (which single mode never paints).
  const ws = { ...twoPaneWs([CHAT('a')], [APP(42)]), viewMode: 'single' }
  const out = resolveWorkspaceRequest(ws, req(APP(42), null, PLACE_WITH_FOCUS, ACTIVATE_FOREGROUND), env(ws))
  assert.deepEqual(out.singleScreen, { kind: 'app', id: '42' })
  assert.equal(out.panes, ws.panes, 'no tree focus/activate mutation')
})

test('single mode + FOREGROUND chat: the chat becomes the slot', () => {
  const ws = { ...twoPaneWs([CHAT('a')], [APP(42)]), viewMode: 'single' }
  const out = resolveWorkspaceRequest(ws, req(CHAT('z'), null, PLACE_WITH_FOCUS, ACTIVATE_FOREGROUND), env(ws))
  assert.deepEqual(out.singleScreen, { kind: 'chat', id: 'z' })
  assert.equal(out.panes, ws.panes)
})

test('single mode + BACKGROUND: parks in the builder tree (workshop), slot untouched', () => {
  const ws = { ...twoPaneWs([CHAT('a')], [APP(42)]), viewMode: 'single', singleScreen: { kind: 'app', id: '42' } }
  const out = resolveWorkspaceRequest(ws, req(APP(9), CHAT('a'), PLACE_BESIDE_SOURCE, ACTIVATE_IN_BACKGROUND), env(ws))
  assert.ok(paneModel.paneOf(out, 'app:9'), 'a background open still lands in the tree')
  assert.deepEqual(out.singleScreen, { kind: 'app', id: '42' }, 'the slot is unchanged by background work')
})

test('builder mode + FOREGROUND: unchanged tree placement (no slot write)', () => {
  const ws = twoPaneWs([CHAT('a')], [APP(42)]) // viewMode panes
  const out = resolveWorkspaceRequest(ws, req(APP(9), null, PLACE_WITH_FOCUS, ACTIVATE_FOREGROUND), env(ws))
  assert.ok(paneModel.paneOf(out, 'app:9'), 'builder foreground opens the pane tab as before')
  assert.equal('singleScreen' in out, false, 'no slot write in builder')
})

// ── the durable-list reconnect wiring is still in place ─────────────────────

test('shell reconciles both durable drawer lists whenever the system stream reconnects', () => {
  const shellSource = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
  assert.match(shellSource, /const reconcileSystemStateOnOpen = useCallback/)
  assert.match(shellSource, /const SYSTEM_RECONNECT_LIST_TIMEOUT_MS = 5_000/)
  assert.match(shellSource, /reconcileSystemStateOnOpen[\s\S]*refreshApps\(\{ timeoutMs: SYSTEM_RECONNECT_LIST_TIMEOUT_MS \}\)/)
  assert.match(shellSource, /reconcileSystemStateOnOpen[\s\S]*fetchFreshChats\(\{ timeoutMs: SYSTEM_RECONNECT_LIST_TIMEOUT_MS \}\)/)
  assert.match(shellSource, /reconcileSystemStateOnOpen[\s\S]*withoutSettledLocalChatRuns/,
    'fresh reconnect truth retires a local active-work marker whose finish event was missed')
  assert.match(shellSource, /acknowledgedIds: acknowledgedLocalChatRunIdsRef\.current[\s\S]*protectedIds: visibleChatIdsRef\.current/,
    'only acknowledged, non-visible local activity can retire from compact truth')
  assert.match(shellSource, /if \(reconciledLocalIds\.has\(chatId\)\) continue/,
    'a mounted local start remains authoritative over a temporarily idle row')
  assert.match(shellSource, /reconcileSystemStateOnOpen[\s\S]*chat\.running[\s\S]*visibleChatIdsRef\.current[\s\S]*markChatRunReconcile\(chatId\)/,
    'open chats and durable running chats reconcile even when run events were missed')
  assert.match(shellSource, /useSystemEventStream\(handleSystemEvent, \{ onOpen: reconcileSystemStateOnOpen \}\)/)
})

test('stale pending updates offer the canonical review surface', () => {
  const shellSource = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
  assert.match(shellSource, /ev\.type === 'app_update_stale'/)
  assert.match(shellSource, /appUpdateStaleMessage\(ev\)/)
  assert.match(shellSource, /findAppStoreApp\(appsRef\.current\)/)
  assert.match(shellSource, /label: 'Open App Store'/)
  assert.match(shellSource, /navToRef\.current\('canvas', \{ appId: appStore\.id \}\)/)
})
