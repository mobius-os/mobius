import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  appSourceProject,
  appSourceProjectId,
  parseAppSourceProjectId,
} from '../appSourceProject.js'

test('app source workspaces reuse project tabs without becoming project records', () => {
  assert.equal(appSourceProjectId(63), 'app-source:63')
  assert.equal(parseAppSourceProjectId('app-source:63'), '63')
  assert.equal(parseAppSourceProjectId('ordinary-project'), null)
  assert.deepEqual(appSourceProject({ id: 63, name: 'LaTeX' }), {
    id: 'app-source:63',
    name: 'LaTeX · Source',
    source_kind: 'app',
    source_app_id: '63',
    app: { id: 63, name: 'LaTeX' },
  })
})
