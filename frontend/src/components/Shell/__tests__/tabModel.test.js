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

// The review's HIGH finding: an app tab MUST navigate with a numeric appId, or
// the iframe LRU dedups on strict !== and double-mounts the app.
test('tabNavTarget gives apps a numeric appId and chats a string chatId', () => {
  const appTarget = tabModel.tabNavTarget(tabModel.makeTab('app', '42'))
  assert.deepEqual(appTarget, { view: 'canvas', opts: { appId: 42 } })
  assert.equal(typeof appTarget.opts.appId, 'number')

  const chatTarget = tabModel.tabNavTarget(tabModel.makeTab('chat', 'abc'))
  assert.deepEqual(chatTarget, { view: 'chat', opts: { chatId: 'abc' } })
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

// ── Settings tab (builder mode) ─────────────────────────────────────────────

test('settingsTab is the one canonical single-instance tab', () => {
  assert.deepEqual(tabModel.settingsTab(), { kind: 'settings', id: 'settings' })
  assert.equal(tabModel.tabKey(tabModel.settingsTab()), tabModel.SETTINGS_TAB_KEY)
  assert.equal(tabModel.SETTINGS_TAB_KEY, 'settings:settings')
  assert.ok(tabModel.isSettingsTab(tabModel.settingsTab()))
  assert.ok(!tabModel.isSettingsTab(tabModel.makeTab('chat', 'settings')))
})

test('tabNavTarget maps the Settings tab to the settings view with no opts', () => {
  assert.deepEqual(tabModel.tabNavTarget(tabModel.settingsTab()), { view: 'settings' })
})

test('readOpenTabs keeps the legacy projection chat/app-only (drops Settings)', () => {
  const store = fakeStorage(JSON.stringify([
    { kind: 'chat', id: 'a' },
    { kind: 'settings', id: 'settings' },
    { kind: 'app', id: 7 },
  ]))
  assert.deepEqual(tabModel.readOpenTabs(store), [
    { kind: 'chat', id: 'a' },
    { kind: 'app', id: '7' },
  ])
})
