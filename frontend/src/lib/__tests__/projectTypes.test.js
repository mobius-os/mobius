import test from 'node:test'
import assert from 'node:assert/strict'
import {
  globalProjectTemplates,
  normalizeProjectColor,
  projectIdentityTone,
  projectTypeKind,
} from '../projectTypes.js'

test('the global picker keeps only the four intentional project kinds in order', () => {
  const templates = [
    { key: 'latex:document', name: 'LaTeX document' },
    { key: 'webstudio:website', name: 'Website' },
    { key: 'webstudio:visualization', name: 'Interactive visualization' },
    { key: 'blank', name: 'Blank project' },
    { key: 'webstudio:document', name: 'Document' },
    { key: 'webstudio:mini-app', name: 'Mini-app' },
    { key: 'webstudio:spreadsheet', name: 'Spreadsheet' },
    { key: 'webstudio:presentation', name: 'Presentation' },
  ]

  assert.deepEqual(
    globalProjectTemplates(templates).map(template => template.key),
    ['blank', 'webstudio:mini-app', 'webstudio:website', 'latex:document'],
  )
})

test('app-backed project kinds disappear when their app contributes no template', () => {
  assert.deepEqual(
    globalProjectTemplates([
      { key: 'blank', name: 'Blank project' },
      { key: 'webstudio:mini-app', name: 'Mini-app' },
    ]).map(template => template.key),
    ['blank', 'webstudio:mini-app'],
  )
})

test('the global picker exposes one provider per project kind', () => {
  assert.deepEqual(
    globalProjectTemplates([
      { key: 'blank', name: 'Blank project' },
      { key: 'studio-a:website', name: 'Website' },
      { key: 'studio-b:site', name: 'Site' },
    ]).map(template => template.key),
    ['blank', 'studio-a:website'],
  )
})

test('project types keep distinct glyph identities for existing projects', () => {
  assert.equal(projectTypeKind({ project_type: 'latex:document' }), 'latex')
  assert.equal(projectTypeKind({ project_type: 'webstudio:website' }), 'web')
  assert.equal(projectTypeKind({ project_type: 'webstudio:mini-app' }), 'mini-app')
  assert.equal(projectTypeKind({ project_type: 'webstudio:spreadsheet' }), 'sheet')
  assert.equal(projectTypeKind({ project_type: 'webstudio:document' }), 'document')
  assert.equal(projectTypeKind({ project_type: 'webstudio:presentation' }), 'slides')
  assert.equal(projectTypeKind({ project_type: 'github:repository' }), 'github')
})

test('every project identity uses one normalized selectable accent', () => {
  assert.equal(normalizeProjectColor('#3B82F6'), '#3b82f6')
  assert.equal(normalizeProjectColor('blue'), null)
  assert.deepEqual(
    projectIdentityTone({ project_type: 'latex:document', color: '#E11D48' }),
    { kind: 'latex', accent: '#e11d48' },
  )
  assert.deepEqual(
    projectIdentityTone({ project_type: 'webstudio:website' }),
    { kind: 'web', accent: 'var(--accent)' },
  )
})
