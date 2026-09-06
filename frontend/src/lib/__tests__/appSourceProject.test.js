import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  appSourceProject,
  appSourceProjectId,
  appForSourceImport,
  importedAppForProject,
  importedAppId,
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

test('app imports resolve the installed app instead of creating a copied project', () => {
  const app = { id: 123, name: 'Rezervacije' }
  assert.equal(appForSourceImport({ kind: 'app', id: '123' }, [app]), app)
  assert.equal(appForSourceImport({ kind: 'artifact', id: '123' }, [app]), null)
})

test('legacy copied app projects resolve to their installed app without deleting the copy', () => {
  const project = {
    id: 'legacy-copy',
    template: { imported_from: { kind: 'app', id: 123 } },
  }
  const app = { id: 123, name: 'Rezervacije' }
  assert.equal(importedAppId(project), '123')
  assert.equal(importedAppForProject(project, new Map([['123', app]])), app)
  assert.equal(importedAppForProject(project, new Map()), null)
  assert.equal(importedAppForProject({ id: 'plain' }, new Map([['123', app]])), null)
  assert.equal(importedAppId({ template: { imported_from: { kind: 'artifact', id: 123 } } }), null)
})
