import test from 'node:test'
import assert from 'node:assert/strict'
import { globalProjectTemplates, projectAppTemplates, defaultProjectName, normalizeProjectColor, projectIdentityTone, projectTypeKind } from '../projectTypes.js'

const core = [{ key: 'blank', name: 'Blank project', kind: 'blank' }, { key: 'app', name: 'App project', kind: 'mini-app' }]

test('core projects do not depend on an installed provider', () => {
  assert.deepEqual(globalProjectTemplates(core).map(t => t.key), ['blank', 'app'])
  assert.deepEqual(projectAppTemplates(core), [])
})

test('installed apps compose directly with the core, without a kind whitelist or first-provider winner', () => {
  const providers = [
    { key: 'latex:document', source_app_id: 63, kind: 'latex' },
    { key: 'webstudio:website', source_app_id: 68, kind: 'web' },
    { key: 'another:website', source_app_id: 80, kind: 'web' },
    { key: 'games:scene', name: 'Game', source_app_id: 90, kind: 'game' },
  ]
  assert.deepEqual(globalProjectTemplates([...providers, ...core]).map(t => t.key), [...core, ...providers].map(t => t.key))
  assert.deepEqual(projectAppTemplates([...core, ...providers]), providers)
  assert.equal(defaultProjectName(providers[3]), 'Untitled game')
})

test('retired formats stay out of new project creation', () => {
  assert.deepEqual(globalProjectTemplates([...core, {key: 'old:deck', source_app_id: 68, retired: true}]), core)
})

test('existing projects retain their type identity and owner color', () => {
  for (const [project_type, kind] of [['latex:document','latex'], ['webstudio:website','web'], ['webstudio:mini-app','mini-app'], ['webstudio:spreadsheet','sheet'], ['webstudio:document','document'], ['webstudio:presentation','slides'], ['github:repository','github']]) {
    assert.equal(projectTypeKind({project_type}), kind)
  }
  assert.equal(projectTypeKind({kind:'game',name:'Website simulator'}), 'game')
  assert.equal(normalizeProjectColor('#3B82F6'), '#3b82f6')
  assert.equal(normalizeProjectColor('blue'), null)
  assert.deepEqual(projectIdentityTone({project_type:'latex:document',color:'#E11D48'}), {kind:'latex',accent:'#e11d48'})
  assert.equal(defaultProjectName(core[0]), 'Untitled project')
})
