import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as tabModel from '../tabModel.js'

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

// ── Apps launcher tab ───────────────────────────────────────────────────────

test('appsTab is one canonical workspace item with an ordinary nav target', () => {
  assert.deepEqual(tabModel.appsTab(), { kind: 'apps', id: 'apps' })
  assert.equal(tabModel.tabKey(tabModel.appsTab()), tabModel.APPS_TAB_KEY)
  assert.equal(tabModel.APPS_TAB_KEY, 'apps:apps')
  assert.ok(tabModel.isAppsTab(tabModel.appsTab()))
  assert.ok(!tabModel.isAppsTab(tabModel.makeTab('app', '1')))
  assert.deepEqual(tabModel.tabNavTarget(tabModel.appsTab()), { view: 'apps' })
})

test('Projects launcher is canonical and project tabs retain string identity', () => {
  assert.deepEqual(tabModel.projectsTab(), { kind: 'projects', id: 'projects' })
  assert.equal(tabModel.tabKey(tabModel.projectsTab()), tabModel.PROJECTS_TAB_KEY)
  assert.ok(tabModel.isProjectsTab(tabModel.projectsTab()))
  assert.deepEqual(tabModel.tabNavTarget(tabModel.projectsTab()), { view: 'projects' })

  const project = tabModel.projectTab('project-1')
  assert.deepEqual(project, { kind: 'project', id: 'project-1' })
  assert.deepEqual(tabModel.tabNavTarget(project), {
    view: 'project', opts: { projectId: 'project-1' },
  })
})
