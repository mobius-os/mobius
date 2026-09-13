// Source menu labels reflect actual project membership, never a copied workspace.
import test from 'node:test'
import assert from 'node:assert/strict'
import { projectSourceAction } from '../projectSourceAction.js'

const source = { kind: 'app', id: '1', name: 'sase-berza' }
const sources = { management: 'linked', apps: [source], artifacts: [] }
const project = { id: 'existing-project', template: { imported_from: { ...source, management: 'linked' } } }

test('unmanaged source offers import only with the linked-source server contract', () => {
  assert.deepEqual(projectSourceAction([], sources, 'app', 1), { label: 'Import to Projects', source })
  assert.equal(projectSourceAction([], { apps: [source] }, 'app', 1), null)
  assert.equal(projectSourceAction([], undefined, 'app', 1), null)
})

test('existing project wins over stale import eligibility and returns the same record', () => {
  const action = projectSourceAction([project], sources, 'app', 1)
  assert.equal(action.label, 'Show in Projects')
  assert.equal(action.project, project)
  assert.equal(action.source, undefined)
  assert.equal(projectSourceAction([project], undefined, 'app', '1').project, project)
})

test('pre-linked import metadata has no current source-management behavior', () => {
  const ordinary = { ...project, template: { imported_from: source } }
  assert.equal(projectSourceAction([ordinary], sources, 'app', 1).label, 'Import to Projects')
})

test('builder app identity and unrelated source kinds do not imply project membership', () => {
  const unrelated = { id: 'other', source_app_id: 1 }
  assert.equal(projectSourceAction([unrelated], sources, 'app', 1).label, 'Import to Projects')
  assert.equal(projectSourceAction([project], sources, 'artifact', 1), null)
  assert.equal(projectSourceAction([project], sources, 'chat', 1), null)
  assert.equal(projectSourceAction([project], sources, 'app', null), null)
})
