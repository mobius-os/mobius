// Key requests are asked in the moment: an agent's on the card saved in the
// chat that asked, an app's in a dialog over the asking app, with the app
// list marking an app whose request waits and Settings › Keys listing the
// agents' requests it answers. The over-app card shows only that app's
// pending asks.
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { appKeyRequestsToShow, appsWaitingForKeys, settingsKeyRequests } = await vite.ssrLoadModule('/src/lib/keyRequestPlaces.js')
const { requestAppPaused } = await vite.ssrLoadModule('/src/lib/keysSettingsText.js')
after(() => vite.close())

const TILES = { host: 'tiles.example.com', methods: ['GET'], paths: ['/*'] }
const agentAsk = {
  id: 'agent-maps', requester: 'agent', app: null, key: 'maps', sites: [TILES],
  status: 'pending', ticket: 't1', chat_id: 'chat-1', key_exists: true,
}
const appAsk = (app, key, extra = {}) => ({
  id: `${app}-${key}`, requester: 'app', app, key, status: 'pending', ticket: `t-${key}`, sites: [TILES], ...extra,
})

test("an app's window shows only that app's pending requests", () => {
  const data = { requests: [appAsk(7, 'maps'), appAsk(8, 'other'), appAsk(7, 'old', { status: 'denied' }), agentAsk] }
  assert.deepEqual(appKeyRequestsToShow(data, 7).map(r => r.id), ['7-maps'])
  // The shell names apps by string id in its tabs.
  assert.deepEqual(appKeyRequestsToShow(data, '7').map(r => r.id), ['7-maps'])
  assert.deepEqual(appKeyRequestsToShow(undefined, 7), [])
})

test('the app list marks every app with a pending request, and no agent or answered one', () => {
  const data = { requests: [appAsk(7, 'maps'), appAsk(8, 'old', { status: 'denied' }), appAsk(9, 'x'), agentAsk] }
  assert.deepEqual([...appsWaitingForKeys(data)].sort(), ['7', '9'])
  assert.deepEqual([...appsWaitingForKeys(undefined)], [])
})

test("Settings › Keys answers only an agent's request that names no chat; every other ask is answered where it was made", () => {
  const chatless = { ...agentAsk, id: 'agent-old', chat_id: null }
  const data = { requests: [appAsk(7, 'maps'), { ...agentAsk, chat_id: 'chat-1' }, chatless, { ...chatless, id: 'agent-no', status: 'denied' }] }
  assert.deepEqual(settingsKeyRequests(data).map(r => r.id), ['agent-old'])
  assert.deepEqual(settingsKeyRequests(undefined), [])
})

test('a switched-off app asking for more websites is marked paused on its card', () => {
  const keyList = [{ name: 'maps', grants: { apps: [{ id: 7, sites: [], paused: true }] } }]
  assert.equal(requestAppPaused(keyList, appAsk(7, 'maps')), true)
  assert.equal(requestAppPaused(keyList, appAsk(8, 'maps')), false)
  assert.equal(requestAppPaused(undefined, appAsk(7, 'maps')), false)
})
