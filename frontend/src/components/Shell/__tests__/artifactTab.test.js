import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as tabModel from '../tabModel.js'
import * as paneModel from '../paneModel.js'

// ── tabModel: the artifact tab is a composite <projectId>:<artifactId> ─────────

test('artifactTab builds a composite id and round-trips through parse', () => {
  const tab = tabModel.artifactTab(12, 'site')
  assert.deepEqual(tab, { kind: 'artifact', id: '12:site' })
  assert.ok(tabModel.isArtifactTab(tab))
  assert.deepEqual(tabModel.parseArtifactTabId('12:site'), { projectId: '12', artifactId: 'site' })
})

test('parseArtifactTabId splits on the FIRST colon and rejects malformed ids', () => {
  assert.deepEqual(tabModel.parseArtifactTabId('7:my-doc'), { projectId: '7', artifactId: 'my-doc' })
  assert.equal(tabModel.parseArtifactTabId('nocolon'), null)
  assert.equal(tabModel.parseArtifactTabId(':site'), null)
  assert.equal(tabModel.parseArtifactTabId('12:'), null)
})

test('tabNavTarget maps an artifact tab to the artifact view with both ids', () => {
  assert.deepEqual(tabModel.tabNavTarget(tabModel.artifactTab(3, 'thesis')), {
    view: 'artifact', opts: { projectId: '3', artifactId: 'thesis' },
  })
})

// ── paneModel: artifact tabs survive normalize + project the right route ───────

test('a workspace seeded with an artifact tab keeps it and routes it', () => {
  const ws = paneModel.seedFromFlatTabs([tabModel.artifactTab(12, 'site')])
  const key = tabModel.tabKey(tabModel.artifactTab(12, 'site'))
  assert.ok(paneModel.paneOf(ws, key), 'artifact tab is a live pane member')

  const route = paneModel.focusedContentRoute(ws)
  assert.equal(route.view, 'artifact')
  assert.equal(route.artifactRef, '12:site')
  assert.equal(route.projectId, '12')
})

test('a malformed artifact tab is dropped by sanitize', () => {
  const ws = paneModel.seedFromFlatTabs([{ kind: 'artifact', id: 'nocolon' }])
  assert.equal(paneModel.paneOf(ws, 'artifact:nocolon'), null)
})

test('the single-screen slot carries an artifact and reports its key + route', () => {
  let ws = paneModel.seedFromFlatTabs([])
  ws = paneModel.setSingleScreen(ws, { kind: 'artifact', id: '4:paper' })
  assert.equal(paneModel.singleScreenKey(ws), 'artifact:4:paper')

  const route = paneModel.singleScreenRoute(ws)
  assert.equal(route.view, 'artifact')
  assert.equal(route.artifactRef, '4:paper')
  assert.equal(route.projectId, '4')
})

test('a corrupt artifact slot collapses to the empty home, never a stray tab', () => {
  let ws = paneModel.seedFromFlatTabs([])
  ws = paneModel.setSingleScreen(ws, { kind: 'artifact', id: 'bad' })
  assert.equal(paneModel.singleScreenKey(ws), null)
})
