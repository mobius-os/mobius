import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as tabModel from '../tabModel.js'

// A Map-backed sessionStorage stub so read/write are testable without jsdom.
function fakeStorage(initial = null) {
  let value = initial
  return {
    getItem: () => value,
    setItem: (_k, v) => { value = v },
    _raw: () => value,
  }
}

test('makeTab normalizes the id to a string', () => {
  assert.deepEqual(tabModel.makeTab('app', 42), { kind: 'app', id: '42' })
  assert.deepEqual(tabModel.makeTab('chat', 'abc'), { kind: 'chat', id: 'abc' })
})

test('sameTab matches across string/number id forms', () => {
  const tab = tabModel.makeTab('app', 42)
  assert.ok(tabModel.sameTab(tab, 'app', 42))
  assert.ok(tabModel.sameTab(tab, 'app', '42'))
  assert.ok(!tabModel.sameTab(tab, 'chat', 42))
  assert.ok(!tabModel.sameTab(tab, 'app', 7))
})

test('addTab dedups by kind+id and does not grow on re-add', () => {
  let tabs = tabModel.addTab([], 'app', 42)
  tabs = tabModel.addTab(tabs, 'app', '42') // same tab, string form
  assert.equal(tabs.length, 1)
  // Different kind, same id, is a distinct tab.
  tabs = tabModel.addTab(tabs, 'chat', '42')
  assert.equal(tabs.length, 2)
})

test('addTab keeps only the most recent MAX_TABS', () => {
  let tabs = []
  for (let i = 0; i < tabModel.MAX_TABS + 3; i++) tabs = tabModel.addTab(tabs, 'chat', i)
  assert.equal(tabs.length, tabModel.MAX_TABS)
  // The oldest were dropped; the newest kept.
  assert.equal(tabs.at(-1).id, String(tabModel.MAX_TABS + 2))
  assert.ok(!tabs.some(t => t.id === '0'))
})

test('addBuiltAppForChat inserts a runnable artifact immediately after its chat', () => {
  const tabs = [
    tabModel.makeTab('app', 1),
    tabModel.makeTab('chat', 'building-chat'),
    tabModel.makeTab('chat', 'other-chat'),
  ]
  assert.deepEqual(tabModel.addBuiltAppForChat(tabs, 'building-chat', 42), [
    tabModel.makeTab('app', 1),
    tabModel.makeTab('chat', 'building-chat'),
    tabModel.makeTab('app', 42),
    tabModel.makeTab('chat', 'other-chat'),
  ])
})

test('addBuiltAppForChat pins the owning chat when it was not open', () => {
  assert.deepEqual(tabModel.addBuiltAppForChat([], 'building-chat', 42), [
    tabModel.makeTab('chat', 'building-chat'),
    tabModel.makeTab('app', 42),
  ])
})

test('addBuiltAppForChat is a strict no-op for recompiles', () => {
  const tabs = [
    tabModel.makeTab('chat', 'building-chat'),
    tabModel.makeTab('app', 42),
    tabModel.makeTab('chat', 'other-chat'),
  ]
  assert.equal(tabModel.addBuiltAppForChat(tabs, 'building-chat', 42), tabs)
})

test('addBuiltAppForChat preserves the new relationship at the flat tab cap', () => {
  const tabs = Array.from(
    { length: tabModel.MAX_TABS },
    (_, index) => tabModel.makeTab('chat', `old-${index}`),
  )
  const after = tabModel.addBuiltAppForChat(tabs, 'building-chat', 42)
  assert.equal(after.length, tabModel.MAX_TABS)
  assert.ok(after.some(tab => tabModel.sameTab(tab, 'chat', 'building-chat')))
  assert.ok(after.some(tab => tabModel.sameTab(tab, 'app', 42)))
})

test('addBuiltAppsForChats handles every app in one refresh in server order', () => {
  const after = tabModel.addBuiltAppsForChats([], [
    { chatId: 'building-chat', appId: 41 },
    { chatId: 'building-chat', appId: 42 },
  ])
  assert.deepEqual(after, [
    tabModel.makeTab('chat', 'building-chat'),
    tabModel.makeTab('app', 41),
    tabModel.makeTab('app', 42),
  ])
})

test('removeTab drops the matching tab and nothing else', () => {
  const tabs = [tabModel.makeTab('chat', 'a'), tabModel.makeTab('app', 42)]
  const after = tabModel.removeTab(tabs, 'app', 42)
  assert.deepEqual(after, [tabModel.makeTab('chat', 'a')])
})

// The review's HIGH finding: an app tab MUST navigate with a numeric appId, or
// the iframe LRU dedups on strict !== and double-mounts the app.
test('tabNavTarget gives apps a numeric appId and chats a string chatId', () => {
  const appTarget = tabModel.tabNavTarget(tabModel.makeTab('app', '42'))
  assert.deepEqual(appTarget, { view: 'canvas', opts: { appId: 42 } })
  assert.equal(typeof appTarget.opts.appId, 'number')

  const chatTarget = tabModel.tabNavTarget(tabModel.makeTab('chat', 'abc'))
  assert.deepEqual(chatTarget, { view: 'chat', opts: { chatId: 'abc' } })
})

test('isTabActive compares a tab against the current view', () => {
  const appTab = tabModel.makeTab('app', 42)
  assert.ok(tabModel.isTabActive(appTab, { view: 'canvas', appId: 42 }))
  assert.ok(tabModel.isTabActive(appTab, { view: 'canvas', appId: '42' }))
  assert.ok(!tabModel.isTabActive(appTab, { view: 'chat', chatId: '42' }))

  const chatTab = tabModel.makeTab('chat', 'abc')
  assert.ok(tabModel.isTabActive(chatTab, { view: 'chat', chatId: 'abc' }))
  assert.ok(!tabModel.isTabActive(chatTab, { view: 'canvas', appId: 'abc' }))
})

test('readOpenTabs restores valid tabs and drops malformed entries', () => {
  const store = fakeStorage(JSON.stringify([
    { kind: 'chat', id: 'a' },
    { kind: 'app', id: 42 },      // numeric id in the store -> normalized
    { kind: 'bogus', id: 'x' },   // unknown kind -> dropped
    { id: 'no-kind' },            // missing kind -> dropped
    { kind: 'app' },              // missing id -> dropped
    'garbage',                    // non-object -> dropped
  ]))
  assert.deepEqual(tabModel.readOpenTabs(store), [
    { kind: 'chat', id: 'a' },
    { kind: 'app', id: '42' },
  ])
})

// A corrupt/hand-edited store must not produce a NaN app id (which navTo would
// turn into /api/apps/NaN and the numeric iframe LRU would accumulate).
test('readOpenTabs drops app tabs whose id is not a finite number', () => {
  const store = fakeStorage(JSON.stringify([
    { kind: 'app', id: 'abc' },   // non-numeric app id -> dropped
    { kind: 'app', id: '7' },     // kept
    { kind: 'chat', id: 'abc' },  // chat ids are free-form -> kept
  ]))
  assert.deepEqual(tabModel.readOpenTabs(store), [
    { kind: 'app', id: '7' },
    { kind: 'chat', id: 'abc' },
  ])
})

// Two id forms of the same tab in a corrupt store would render duplicate keys.
test('readOpenTabs dedups the same tab across id forms', () => {
  const store = fakeStorage(JSON.stringify([
    { kind: 'app', id: 42 },
    { kind: 'app', id: '42' },    // same tab, string form -> deduped
    { kind: 'chat', id: 'a' },
  ]))
  assert.deepEqual(tabModel.readOpenTabs(store), [
    { kind: 'app', id: '42' },
    { kind: 'chat', id: 'a' },
  ])
})

test('readOpenTabs returns [] for absent, non-array, or corrupt storage', () => {
  assert.deepEqual(tabModel.readOpenTabs(fakeStorage(null)), [])
  assert.deepEqual(tabModel.readOpenTabs(fakeStorage('{"not":"an array"}')), [])
  assert.deepEqual(tabModel.readOpenTabs(fakeStorage('not json')), [])
})

test('writeOpenTabs round-trips through readOpenTabs', () => {
  const store = fakeStorage()
  const tabs = [tabModel.makeTab('chat', 'a'), tabModel.makeTab('app', 42)]
  tabModel.writeOpenTabs(tabs, store)
  assert.deepEqual(tabModel.readOpenTabs(store), tabs)
})
