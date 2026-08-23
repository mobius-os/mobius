import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  addCreatedChatToList,
  createdChatDetailCache,
  currentReusableEmptyChat,
  detailIsUntouchedEmptyChat,
  enteredEmptySingleScreen,
  clearNewChatIntent,
  mergeChatListWithCreatedGuards,
  mintNewChatIntentId,
  newChatPresentationCoversSurface,
  newChatPresentationIsCurrent,
  readNewChatIntent,
  reconcileCreatedChatGuard,
  reconcileNewChatIntentCreate,
  rememberCreatedChat,
  resolveNewChatIntentId,
  reusableChatDetailVerdict,
  validNewChatIntentId,
  writeNewChatIntent,
} from '../newChatPolicy.js'
import { chatQueries } from '../../../hooks/queries.js'

const shellSource = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
const queriesSource = readFileSync(new URL('../../../hooks/queries.js', import.meta.url), 'utf8')
const clientSource = readFileSync(new URL('../../../api/client.js', import.meta.url), 'utf8')

const empty = (id, extra = {}) => ({
  id,
  has_messages: false,
  running: false,
  ...extra,
})

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

test('New Chat intent id: valid UUID, unique, injectable, fail-safe', () => {
  const a = mintNewChatIntentId()
  const b = mintNewChatIntentId()
  assert.match(a, UUID_RE)
  assert.match(b, UUID_RE)
  assert.notEqual(a, b)
  // A valid injected source is used verbatim (client id == future chat id).
  assert.equal(
    mintNewChatIntentId({ randomUUID: () => '11111111-1111-4111-8111-111111111111' }),
    '11111111-1111-4111-8111-111111111111',
  )
  // A throwing or malformed source falls back to a valid manual v4 mint.
  assert.match(mintNewChatIntentId({ randomUUID: () => { throw new Error('nope') } }), UUID_RE)
  assert.match(mintNewChatIntentId({ randomUUID: () => 'not-a-uuid' }), UUID_RE)
})

test('resolveNewChatIntentId resumes every open intent, else mints fresh', () => {
  const mint = () => 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
  const pending = { chatId: '99999999-9999-4999-8999-999999999999', status: 'allocating' }
  // The pointer is independent of Web Storage: keep the id even when the
  // synchronous draft mirror is unavailable so IndexedDB can hydrate it.
  assert.equal(
    resolveNewChatIntentId(pending, { randomUUID: mint }),
    '99999999-9999-4999-8999-999999999999',
  )
  // Server allocation is not completion: away/back must still find the draft.
  assert.equal(
    resolveNewChatIntentId({ ...pending, status: 'materialized' }, { randomUUID: mint }),
    '99999999-9999-4999-8999-999999999999',
  )
  assert.equal(
    resolveNewChatIntentId({ ...pending, status: 'retired' }, { randomUUID: mint }),
    mint(),
  )
  // No pending intent -> fresh id.
  assert.equal(resolveNewChatIntentId(null, { randomUUID: mint }), mint())
})

test('reconcileNewChatIntentCreate rotates only on authoritative conflicts', () => {
  const mint = () => 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
  const id = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
  assert.deepEqual(
    reconcileNewChatIntentCreate(id, 'empty', { randomUUID: mint }),
    { action: 'accept', chatId: id },
  )
  for (const verdict of ['occupied', 'tombstoned']) {
    assert.deepEqual(
      reconcileNewChatIntentCreate(id, verdict, { randomUUID: mint }),
      { action: 'rotate', chatId: mint() },
    )
  }
  for (const verdict of ['missing', 'uncertain', 'error']) {
    assert.deepEqual(
      reconcileNewChatIntentCreate(id, verdict, { randomUUID: mint }),
      { action: 'retry', chatId: id },
    )
  }
})

test('a superseded create waiter cannot rotate the reopened New Chat draft', () => {
  const settle = shellSource.match(
    /async function settleDraftFirstNewChat\(presentation\) \{([\s\S]*?)\n  \}\n\n  settleDraftFirstNewChatRef\.current/,
  )?.[1] || ''
  const rotate = settle.match(
    /if \(decision\.action === 'rotate'\) \{([\s\S]*?)\n    \}\n\n    if \(decision\.action !== 'accept'\)/,
  )?.[1] || ''

  const ownerCheck = rotate.indexOf(
    'if (!draftFirstPresentationIsCurrent(presentation)) return',
  )
  const intentCheck = rotate.indexOf(
    "if (String(newChatIntentRef.current?.chatId ?? '') !== intentId) return",
  )
  const draftRead = rotate.indexOf('readComposerDraft(intentId)')
  const durableDraftRead = rotate.indexOf('await readComposerDraftAsync(intentId)')
  const draftCopy = rotate.indexOf('persistComposerDraft(')
  const pointerMove = rotate.indexOf(
    "rememberOpenNewChatIntent({ chatId: decision.chatId, status: 'allocating' })",
  )

  assert.ok(ownerCheck >= 0, 'rotation must claim the live presentation')
  for (const [name, position] of [
    ['intent ownership check', intentCheck],
    ['durable draft read', durableDraftRead],
    ['draft copy', draftCopy],
    ['intent pointer move', pointerMove],
  ]) {
    assert.ok(position > ownerCheck,
      `${name} must happen only after the waiter claims the live presentation`)
  }
  assert.ok(draftRead < 0, 'conflict rotation must not trust only the synchronous mirror')
  const checksBeforeRead = rotate.slice(0, durableDraftRead)
    .match(/draftFirstPresentationIsCurrent\(presentation\)/g)?.length || 0
  const checksAfterRead = rotate.slice(durableDraftRead, draftCopy)
    .match(/draftFirstPresentationIsCurrent\(presentation\)/g)?.length || 0
  assert.equal(checksBeforeRead, 1)
  assert.equal(checksAfterRead, 1,
    'rotation must reclaim presentation ownership after durable hydration')
})

test('an accepted allocation hydrates durable draft ownership before handoff', () => {
  const settle = shellSource.match(
    /async function settleDraftFirstNewChat\(presentation\) \{([\s\S]*?)\n  \}\n\n  settleDraftFirstNewChatRef\.current/,
  )?.[1] || ''
  const accepted = settle.slice(settle.indexOf("if (decision.action !== 'accept')"))
  const hydrate = accepted.indexOf('await readComposerDraftAsync(intentId)')
  const currentAgain = accepted.indexOf(
    'if (!draftFirstPresentationIsCurrent(presentation)) return', hydrate,
  )
  const route = accepted.indexOf("if (changesRoute) navTo('chat', { chatId: intentId })")
  assert.ok(hydrate >= 0 && currentAgain > hydrate && route > currentAgain,
    'destination navigation must wait for durable hydration and reclaimed ownership')
})

class MemoryStorage {
  constructor(values = {}) { this.values = new Map(Object.entries(values)) }
  getItem(key) { return this.values.get(key) ?? null }
  setItem(key, value) { this.values.set(key, String(value)) }
  removeItem(key) { this.values.delete(key) }
}

test('the open New Chat pointer is valid, tab-scoped, and owner-checked on clear', () => {
  const storage = new MemoryStorage()
  const id = '99999999-9999-4999-8999-999999999999'
  assert.equal(validNewChatIntentId(id), true)
  assert.equal(validNewChatIntentId('not-a-chat'), false)
  assert.equal(writeNewChatIntent({ chatId: id, status: 'materialized' }, storage), true)
  assert.deepEqual(readNewChatIntent(storage), { chatId: id, status: 'materialized' })
  assert.equal(clearNewChatIntent('other', storage), false)
  assert.deepEqual(readNewChatIntent(storage), { chatId: id, status: 'materialized' })
  assert.equal(clearNewChatIntent(id, storage), true)
  assert.equal(readNewChatIntent(storage), null)

  storage.setItem('new-chat-intent', JSON.stringify({ chatId: 'bad', status: 'allocating' }))
  assert.equal(readNewChatIntent(storage), null)
})

test('New Chat intent ids are canonicalized before storage and allocation', () => {
  const storage = new MemoryStorage()
  const upper = 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'
  const lower = upper.toLowerCase()

  assert.equal(mintNewChatIntentId({ randomUUID: () => upper }), lower)
  assert.equal(resolveNewChatIntentId({
    chatId: upper,
    status: 'failed',
  }), lower)
  assert.equal(writeNewChatIntent({ chatId: upper, status: 'allocating' }, storage), true)
  assert.deepEqual(readNewChatIntent(storage), {
    chatId: lower,
    status: 'allocating',
  })
  assert.equal(clearNewChatIntent(upper, storage), true)
})

test('New Chat presentation ownership follows allocation context then destination', () => {
  const presentation = {
    chatId: null,
    materialized: false,
    navigationEpoch: 4,
    viewMode: 'single',
    drawerEntryOpen: true,
  }
  const current = {
    navigationEpoch: 4,
    viewMode: 'single',
    drawerEntryOpen: true,
    activeView: 'chat',
    activeChatId: 'old',
  }

  for (const [change, expected] of [
    [{}, true],
    [{ navigationEpoch: 5 }, false],
    [{ drawerEntryOpen: false }, false],
    [{ viewMode: 'panes' }, false],
  ]) {
    assert.equal(newChatPresentationIsCurrent(presentation, {
      ...current, ...change,
    }), expected)
  }
  assert.equal(newChatPresentationIsCurrent({
    ...presentation, drawerEntryOpen: false,
  }, current), false)
  // A provisional client UUID is still allocation-owned until the server row
  // has been accepted; merely having an id must not adopt route semantics.
  assert.equal(newChatPresentationIsCurrent({
    ...presentation, chatId: 'client-owned',
  }, current), true)
  assert.equal(newChatPresentationIsCurrent({
    ...presentation, chatId: 'client-owned',
  }, { ...current, navigationEpoch: 5 }), false)

  const builderPresentation = {
    ...presentation,
    chatId: 'client-owned',
    viewMode: 'panes',
    paneId: 'left',
    paneActiveKey: 'chat:old',
  }
  const builderCurrent = {
    ...current,
    viewMode: 'panes',
    focusedPaneId: 'left',
    paneActiveKey: 'chat:old',
  }
  assert.equal(newChatPresentationIsCurrent(builderPresentation, builderCurrent), true)
  assert.equal(newChatPresentationIsCurrent(builderPresentation, {
    ...builderCurrent, focusedPaneId: 'right',
  }), false, 'focusing another pane supersedes its provisional composer')
  assert.equal(newChatPresentationIsCurrent(builderPresentation, {
    ...builderCurrent, paneActiveKey: 'app:42',
  }), false, 'selecting another tab in the origin pane supersedes it')

  const resolvedPresentation = {
    chatId: 'new',
    materialized: true,
    navigationEpoch: 4,
    viewMode: 'single',
    drawerEntryOpen: false,
  }
  const resolvedCurrent = {
    navigationEpoch: 9,
    viewMode: 'single',
    drawerEntryOpen: false,
    activeView: 'chat',
    activeChatId: 'new',
  }

  assert.equal(newChatPresentationIsCurrent(resolvedPresentation, resolvedCurrent), true)
  assert.equal(newChatPresentationIsCurrent(resolvedPresentation, {
    ...resolvedCurrent, drawerEntryOpen: true,
  }), false, 'reopening navigation supersedes a materialized cover')
  assert.equal(newChatPresentationIsCurrent(resolvedPresentation, {
    ...resolvedCurrent, activeView: 'canvas', activeChatId: null,
  }), false)
  assert.equal(newChatPresentationIsCurrent(resolvedPresentation, {
    ...resolvedCurrent, activeChatId: 'other',
  }), false)

  // Route still catching up to the resolved chat: activeChatId has not
  // committed yet, but no navigation happened since it resolved (epoch
  // unchanged). The cover stays current so the outgoing chat never flashes
  // between the New chat surface and its destination.
  assert.equal(newChatPresentationIsCurrent(resolvedPresentation, {
    ...resolvedCurrent, navigationEpoch: 4, activeChatId: 'old',
  }), true)
  // A genuine supersede in that same window (navigation bumped the epoch and
  // landed elsewhere) must still retire the cover.
  assert.equal(newChatPresentationIsCurrent(resolvedPresentation, {
    ...resolvedCurrent, navigationEpoch: 5, activeChatId: 'old',
  }), false)
})

test('New Chat cover gates only its owned surface and destination handoff', () => {
  const standard = {
    chatId: 'new',
    materialized: false,
    viewMode: 'single',
    paneId: null,
  }
  assert.equal(newChatPresentationCoversSurface(null, {
    viewMode: 'single', paneId: 'single', chatId: 'old',
  }), false)
  assert.equal(newChatPresentationCoversSurface(standard, {
    viewMode: 'single', paneId: 'single', chatId: 'old',
  }), true)
  assert.equal(newChatPresentationCoversSurface({ ...standard, materialized: true }, {
    viewMode: 'single', paneId: 'single', chatId: 'new',
  }), false, 'the matching destination becomes interactive for handoff')
  assert.equal(newChatPresentationCoversSurface({ ...standard, materialized: true }, {
    viewMode: 'single', paneId: 'single', chatId: 'old',
  }), true)

  const builder = {
    chatId: 'new',
    materialized: false,
    viewMode: 'panes',
    paneId: 'left',
  }
  assert.equal(newChatPresentationCoversSurface(builder, {
    viewMode: 'panes', paneId: 'left', chatId: 'old',
  }), true)
  assert.equal(newChatPresentationCoversSurface(builder, {
    viewMode: 'panes', paneId: 'right', chatId: 'sibling',
  }), false, 'Builder sibling panes remain usable')
  assert.equal(newChatPresentationCoversSurface(builder, {
    viewMode: 'single', paneId: 'single', chatId: 'old',
  }), true, 'a stale world stays blocked until layout-effect retirement')
})

test('empty-single policy fires only on the transition edge', () => {
  const chat = { kind: 'chat', id: '7' }
  assert.equal(enteredEmptySingleScreen(
    { viewMode: 'panes', singleScreen: null },
    { viewMode: 'single', singleScreen: null },
  ), true)
  assert.equal(enteredEmptySingleScreen(
    { viewMode: 'single', singleScreen: chat },
    { viewMode: 'single', singleScreen: null },
  ), true)
  assert.equal(enteredEmptySingleScreen(
    { viewMode: 'single', singleScreen: null },
    { viewMode: 'single', singleScreen: null },
  ), false)
  assert.equal(enteredEmptySingleScreen(
    { viewMode: 'panes', singleScreen: chat },
    { viewMode: 'panes', singleScreen: null },
  ), false)
})

test('only the active empty chat is eligible for client-side reuse', () => {
  const offscreen = empty('offscreen')
  const active = empty('active')

  assert.equal(currentReusableEmptyChat([offscreen, active], {
    activeChatId: 'active',
  }), active)
  assert.equal(currentReusableEmptyChat([offscreen], {
    activeChatId: 'active',
  }), null)
})

test('running, recovered, streaming, and populated active chats are rejected', () => {
  const options = { activeChatId: 'active' }
  assert.equal(currentReusableEmptyChat([empty('active', { running: true })], options), null)
  assert.equal(currentReusableEmptyChat([empty('active', { has_messages: true })], options), null)
  assert.equal(currentReusableEmptyChat([empty('active')], {
    ...options, recoveredChatIds: new Set(['active']),
  }), null)
  assert.equal(currentReusableEmptyChat([empty('active')], {
    ...options, streamingChatIds: new Set(['active']),
  }), null)
})

test('id comparison is stable across numeric and string representations', () => {
  const active = empty(7)
  assert.equal(currentReusableEmptyChat([active], {
    activeChatId: '7',
  }), active)
})

function untouchedDetail(extra = {}) {
  return {
    total: 0,
    messages: [],
    pending_messages: [],
    running: false,
    pending_question_id: null,
    session_id: null,
    created_by_app_id: null,
    ...extra,
  }
}

test('fresh detail accepts only a fully untouched empty chat', () => {
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail()), true)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ total: 1 })), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ messages: [{}] })), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ pending_messages: [{}] })), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ running: true })), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ pending_question_id: 'q' })), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ session_id: 'session' })), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ created_by_app_id: 7 })), false)
})

test('fresh detail fails closed on partial or malformed responses', () => {
  assert.equal(detailIsUntouchedEmptyChat(null), false)
  assert.equal(detailIsUntouchedEmptyChat({ messages: [], pending_messages: [] }), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ total: '0' })), false)
  const { created_by_app_id: _owner, ...missingOwner } = untouchedDetail()
  assert.equal(detailIsUntouchedEmptyChat(missingOwner), false)
})

test('fresh detail probe separates occupied, missing, and uncertain rows', () => {
  assert.equal(reusableChatDetailVerdict({
    ok: true, status: 200, detail: untouchedDetail(),
  }), 'empty')
  assert.equal(reusableChatDetailVerdict({
    ok: true, status: 200, detail: untouchedDetail({ total: 1, messages: [{}] }),
  }), 'occupied')
  assert.equal(reusableChatDetailVerdict({
    ok: false, status: 404, detail: null,
  }), 'missing')
  assert.equal(reusableChatDetailVerdict({
    ok: false, status: 503, detail: null,
  }), 'uncertain')
  assert.equal(reusableChatDetailVerdict({
    ok: true, status: 200, detail: { messages: [], pending_messages: [] },
  }), 'uncertain')
})

test('a canonical create response becomes an authoritative empty detail cache', () => {
  const cache = createdChatDetailCache({
    id: 'new',
    detail: untouchedDetail({
      id: 'new',
      provider: 'codex',
      created_by_app_id: null,
      agent_settings_json: null,
      effective_agent_settings: { model: 'gpt-current', effort: 'medium' },
      has_assistant_turns: false,
      auto_resume_on_limit: false,
      offset: 0,
      updated_at: '2026-07-30T12:00:00Z',
    }),
  })

  assert.deepEqual(cache, {
    restorationWindowComplete: true,
    updated_at: '2026-07-30T12:00:00Z',
    messages: [],
    pending_messages: [],
    pending_question_id: null,
    total: 0,
    offset: 0,
    running: false,
    chatInfo: {
      provider: 'codex',
      created_by_app_id: null,
      agent_settings_json: null,
      effective: { model: 'gpt-current', effort: 'medium' },
      has_assistant_turns: false,
      auto_resume_on_limit: false,
    },
  })
})

test('an older partial create response leaves the detail fetch path intact', () => {
  assert.equal(createdChatDetailCache({ id: 'old', messages: [] }), null)
})

test('a created chat enters the cache without displacing pinned chats', () => {
  const updatedAt = '2026-07-20T12:00:00.000Z'
  const result = addCreatedChatToList([
    { id: 'pinned', pinned_at: '2026-07-19T10:00:00.000Z' },
    { id: 'older', pinned_at: null },
  ], {
    id: 'new',
    title: 'New chat',
    updated_at: updatedAt,
    activity_at: null,
    pinned_at: null,
    has_messages: false,
    created_by_app_id: null,
    running: false,
    messages: [],
    detail: untouchedDetail(),
  })

  assert.deepEqual(result.map(chat => chat.id), ['pinned', 'new', 'older'])
  assert.equal(result[1].updated_at, updatedAt)
  assert.equal(result[1].has_messages, false)
  assert.equal('messages' in result[1], false)
  assert.equal('detail' in result[1], false)
})

test('ordinary chat selection does not launch a competing drawer refresh', () => {
  const selectChat = shellSource.match(
    /function selectChat\(id, \{ focusComposer = true \} = \{\}\) \{([\s\S]*?)\n  \}/,
  )?.[1] || ''
  assert.match(selectChat,
    /navTo\('chat', \{ chatId: id, preserveDrawerPresentation \}\)/)
  assert.doesNotMatch(selectChat, /refreshChats/)
})

test('new-chat creation cancels stale list reads through a real AbortSignal', () => {
  const cancelAt = shellSource.indexOf('await queryClient.cancelQueries({')
  const createAt = shellSource.indexOf("api.chats.create({ title: 'New chat' })")
  assert.ok(cancelAt >= 0 && cancelAt < createAt,
    'the stale drawer read must be cancelled before the create request')
  assert.match(queriesSource, /async function fetchChats\(\{ signal \} = \{\}\)/)
  assert.match(queriesSource, /api\.chats\.list\(\{ signal \}\)/)
  assert.match(clientSource, /list: \(options = \{\}\) => apiFetch\('\/chats', options\)/)
})

test('the drawer transport aborts before creation can continue', async () => {
  const originalFetch = globalThis.fetch
  const sequence = []
  globalThis.fetch = (_url, options = {}) => new Promise(resolve => {
    sequence.push('list-started')
    options.signal?.addEventListener('abort', () => {
      sequence.push('list-aborted')
      // Resolve a non-success response instead of rejecting so apiFetch's
      // connectivity verifier does not start unrelated background work.
      resolve(new Response('[]', {
        status: 499,
        headers: { 'Content-Type': 'application/json' },
      }))
    }, { once: true })
  })

  try {
    const controller = new AbortController()
    const list = chatQueries.list.fetch({ signal: controller.signal })
    await Promise.resolve()
    controller.abort()
    await assert.rejects(list, /chats fetch failed: 499/)
    sequence.push('create-allowed')
    assert.deepEqual(sequence, [
      'list-started', 'list-aborted', 'create-allowed',
    ])
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('a created chat replaces a duplicate cache row', () => {
  const result = addCreatedChatToList([
    { id: 'same', title: 'stale', pinned_at: null },
  ], {
    id: 'same', title: 'Fresh', has_messages: true, messages: [{ role: 'user' }],
  })

  assert.equal(result.length, 1)
  assert.equal(result[0].title, 'Fresh')
  assert.equal(result[0].has_messages, true)
})

test('a stale post-create list cannot hide the protected chat row', () => {
  const guards = new Map()
  const created = {
    id: 'new', title: 'New chat', pinned_at: null, has_messages: false,
  }
  rememberCreatedChat(guards, created, { now: 1000, guardMs: 30_000 })

  const stale = mergeChatListWithCreatedGuards([
    { id: 'older', title: 'Older', pinned_at: null },
  ], guards, { now: 2000 })
  assert.deepEqual(stale.map(chat => chat.id), ['new', 'older'])

  const confirmed = mergeChatListWithCreatedGuards([
    { id: 'new', title: 'Server title', pinned_at: null, has_messages: true },
    { id: 'older', title: 'Older', pinned_at: null },
  ], guards, { now: 3000 })
  assert.equal(confirmed[0].title, 'Server title')

  const secondFallback = mergeChatListWithCreatedGuards([
    { id: 'older', title: 'Older', pinned_at: null },
  ], guards, { now: 4000 })
  assert.equal(secondFallback[0].title, 'Server title')

  const expired = mergeChatListWithCreatedGuards([
    { id: 'older', title: 'Older', pinned_at: null },
  ], guards, { now: 31_001 })
  assert.deepEqual(expired.map(chat => chat.id), ['older'])
  assert.equal(guards.size, 0)
})

test('detail verdicts update or retire the protected create row', () => {
  const guards = new Map()
  const created = {
    id: 'new', title: 'New chat', pinned_at: null, has_messages: false,
  }
  rememberCreatedChat(guards, created, { now: 1000, guardMs: 30_000 })

  reconcileCreatedChatGuard(guards, 'new', 'occupied')
  const occupiedFallback = mergeChatListWithCreatedGuards([], guards, {
    now: 2000,
  })
  assert.equal(occupiedFallback[0].has_messages, true)

  reconcileCreatedChatGuard(guards, 'new', 'missing')
  const missingFallback = mergeChatListWithCreatedGuards([], guards, {
    now: 3000,
  })
  assert.deepEqual(missingFallback, [])
  assert.equal(guards.size, 0)
})

test('stale-present rows cannot downgrade an occupied or newer guard', () => {
  const guards = new Map()
  rememberCreatedChat(guards, {
    id: 'new',
    title: 'Created title',
    pinned_at: null,
    has_messages: false,
    updated_at: '2026-07-22T00:00:02Z',
  }, { now: 1000, guardMs: 30_000 })
  reconcileCreatedChatGuard(guards, 'new', 'occupied')

  const stalePresent = mergeChatListWithCreatedGuards([{
    id: 'new',
    title: 'Older cached title',
    pinned_at: null,
    has_messages: false,
    updated_at: '2026-07-22T00:00:01Z',
  }], guards, { now: 2000 })
  assert.equal(stalePresent[0].title, 'Created title')
  assert.equal(stalePresent[0].has_messages, true)

  const newerConfirmed = mergeChatListWithCreatedGuards([{
    id: 'new',
    title: 'Server title',
    pinned_at: null,
    has_messages: true,
    updated_at: '2026-07-22T00:00:03Z',
  }], guards, { now: 3000 })
  assert.equal(newerConfirmed[0].title, 'Server title')
  assert.equal(newerConfirmed[0].has_messages, true)

  const secondStalePresent = mergeChatListWithCreatedGuards([{
    id: 'new',
    title: 'Older cached title',
    pinned_at: null,
    has_messages: false,
    updated_at: '2026-07-22T00:00:01Z',
  }], guards, { now: 4000 })
  assert.equal(secondStalePresent[0].title, 'Server title')
  assert.equal(secondStalePresent[0].has_messages, true)
})
