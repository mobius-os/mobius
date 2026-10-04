import test from 'node:test'
import assert from 'node:assert/strict'
import { APP_GROUPS } from '../walkthroughGroups.js'

test('app_screens_cover_the_five_groups_with_unique_apps_and_complete_copy', () => {
  assert.deepEqual(APP_GROUPS.map(group => group.id), ['system', 'personalize', 'artifacts', 'explore', 'insight'])
  const ids = APP_GROUPS.flatMap(group => group.apps.map(app => app.id))
  assert.equal(new Set(ids).size, ids.length, 'an app appears in exactly one group')
  for (const group of APP_GROUPS) {
    assert.equal(group.title.length, 2, `${group.id} title is [plain, accent]`)
    assert.ok(group.lead && group.eyebrow, `${group.id} has its headline copy`)
    for (const app of group.apps) assert.ok(app.name && app.blurb, `${app.id} has a name and blurb`)
  }
})
