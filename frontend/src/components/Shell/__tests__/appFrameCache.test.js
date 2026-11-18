import assert from 'node:assert/strict'
import test from 'node:test'

import {
  BASE_APP_CACHE_MAX,
  HIGH_MEMORY_APP_CACHE_MAX,
  appFrameCacheMaxForDeviceMemory,
  deriveRenderedAppIds,
} from '../appFrameCache.js'
import * as paneModel from '../paneModel.js'
import * as tabModel from '../tabModel.js'
import { deriveContentVisibility } from '../workspaceView.js'

test('unknown and lower-memory devices keep the established six-frame budget', () => {
  assert.equal(appFrameCacheMaxForDeviceMemory(undefined), BASE_APP_CACHE_MAX)
  assert.equal(appFrameCacheMaxForDeviceMemory(4), BASE_APP_CACHE_MAX)
  assert.equal(BASE_APP_CACHE_MAX, 6)
})

test('the highest reported memory tier retains a larger warm working set', () => {
  assert.equal(appFrameCacheMaxForDeviceMemory(8), HIGH_MEMORY_APP_CACHE_MAX)
  assert.equal(HIGH_MEMORY_APP_CACHE_MAX, 10)
})

test('visible app frames remain mounted even when they exceed the warm limit', () => {
  assert.deepEqual(
    deriveRenderedAppIds({
      visibleAppIds: new Set([8, 3, 5, 2, 7, 1, 4]),
      singleScreen: null,
      warmIds: [9, 10],
      max: 6,
    }),
    ['1', '2', '3', '4', '5', '7', '8'],
  )
})

test('the standard-mode app is pinned while builder is the visible scene', () => {
  assert.deepEqual(
    deriveRenderedAppIds({
      visibleAppIds: new Set(),
      singleScreen: { kind: 'app', id: 12 },
      warmIds: [9, 12, 4, 3],
      max: 3,
    }),
    ['4', '9', '12'],
  )
})

test('warm app frames are deduplicated and bounded behind visible frames', () => {
  assert.deepEqual(
    deriveRenderedAppIds({
      visibleAppIds: new Set([6]),
      singleScreen: null,
      warmIds: ['6', 5, 5, 4, 3],
      max: 3,
    }),
    ['4', '5', '6'],
  )
})

// ── Crash recovery: what the first render after a killed page mounts ──────
// iOS Safari kills a page that runs out of memory and reloads it. The restored
// workspace goes through withoutBootApps before the first render, and the warm
// list is not seeded with the cold-restored app, so no app frame (visible or
// hidden) mounts until the owner opens one.

const CONTENT = { x: 0, y: 0, w: 1400, h: 900 }
const { makeTab } = tabModel

function bootFrames(ws, warmIds = []) {
  const projection = paneModel.projectLayout(ws, paneModel.modeForRect(CONTENT), CONTENT)
  const { visibleAppIds } = deriveContentVisibility({
    workspace: ws, projection, viewMode: ws.viewMode,
    settingsOverlayOpen: false, immersiveActive: false, immersiveAppId: null,
  })
  return deriveRenderedAppIds({ visibleAppIds, singleScreen: ws.singleScreen, warmIds })
}

function standardShowingApp() {
  return paneModel.setSingleScreen(
    paneModel.seedFromFlatTabs([makeTab('chat', '5'), makeTab('app', '9')]),
    makeTab('app', '9'),
  )
}

function builderWithChat(ws) {
  const builder = paneModel.setViewMode(ws, 'panes')
  return paneModel.setActiveTab(builder, builder.focusedPaneId, 'chat:5')
}

test('crash recovery: an app on the Standard screen does not mount', () => {
  const ws = standardShowingApp()
  assert.deepEqual(bootFrames(ws), ['9'], 'without recovery the app mounts')
  const recovered = paneModel.withoutBootApps(ws)
  assert.equal(recovered.singleScreen, null, 'Standard lands on the empty home')
  assert.deepEqual(bootFrames(recovered), [])
  assert.equal(paneModel.flatten(recovered).length, 2, 'its tab stays one tap away')
})

test('crash recovery: an app retained in the Standard slot behind Builder does not mount', () => {
  const ws = builderWithChat(standardShowingApp())
  assert.equal(ws.viewMode, 'panes')
  assert.deepEqual(bootFrames(ws), ['9'], 'the retained slot mounts hidden without recovery')
  const recovered = paneModel.withoutBootApps(ws)
  assert.equal(recovered.viewMode, 'panes', 'a Builder world showing no app keeps Builder')
  assert.deepEqual(bootFrames(recovered), [])
})

test('crash recovery: an app visible in a Builder split does not mount', () => {
  let ws = paneModel.seedFromFlatTabs([makeTab('chat', '5')])
  ws = paneModel.splitPaneWithTab(paneModel.setViewMode(ws, 'panes'), makeTab('app', '42'), {
    paneId: ws.focusedPaneId, edge: 'right',
  })
  ws = paneModel.setSingleScreen(ws, makeTab('chat', '5'))
  assert.deepEqual(bootFrames(ws), ['42'], 'without recovery the split app mounts')
  const recovered = paneModel.withoutBootApps(ws)
  assert.equal(recovered.viewMode, 'single', 'falls back to Standard, showing the chat')
  assert.deepEqual(bootFrames(recovered), [])
  assert.deepEqual(recovered.layout, ws.layout, 'the Builder split is kept for an explicit return')
})

test('crash recovery leaves a workspace that mounts no app unchanged', () => {
  const ws = paneModel.setSingleScreen(
    paneModel.seedFromFlatTabs([makeTab('chat', '5')]), makeTab('chat', '5'),
  )
  assert.equal(paneModel.withoutBootApps(ws), ws)
})
