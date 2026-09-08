import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  appSourceProject,
  appSourceProjectId,
  projectImportSource,
  linkedProjectAppId,
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

test('Add to Projects uses server eligibility, excluding work already managed', () => {
  const source = { kind: 'app', id: 123, name: 'Rezervacije' }
  const sources = { management: 'linked', apps: [source], artifacts: [{ kind: 'artifact', id: 'site' }] }
  assert.equal(projectImportSource(sources, 'app', '123'), source)
  assert.equal(projectImportSource(sources, 'app', 'already-managed'), null)
  assert.equal(projectImportSource(undefined, 'app', '123'), null)
  assert.equal(projectImportSource({ apps: [source] }, 'app', '123'), null, 'old copy backend must not expose Add to Projects')
  assert.equal(projectImportSource({ ...sources, management: 'copy' }, 'app', '123'), null)
  assert.equal(projectImportSource(sources, 'chat', '123'), null)
  assert.equal(projectImportSource(sources, 'artifact', 'site').id, 'site')
})

test('only explicitly linked app Projects are associated with an installed app for Apply', () => {
  assert.equal(linkedProjectAppId({ template: { imported_from: { kind: 'app', id: 123, management: 'linked' } } }), '123')
  assert.equal(linkedProjectAppId({ template: { imported_from: { kind: 'app', id: 123 } } }), null)
  assert.equal(linkedProjectAppId({ template: { imported_from: { kind: 'artifact', id: 123, management: 'linked' } } }), null)
  assert.equal(linkedProjectAppId({ template: { imported_from: { kind: 'app', id: '', management: 'linked' } } }), null)
  assert.equal(linkedProjectAppId({ id: 'plain' }), null)
})
