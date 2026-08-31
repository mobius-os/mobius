import { test } from 'node:test'
import assert from 'node:assert/strict'
import { recentsProjectChip } from '../recentsProjectChip.js'

test('a project-owned chat row yields a chip', () => {
  assert.deepEqual(
    recentsProjectChip('chat', { id: 'c1', title: 'Draft', project: { id: 7, name: 'Thesis' } }),
    { id: '7', name: 'Thesis', color: null },
  )
})

test('a chat with no project yields no chip', () => {
  assert.equal(recentsProjectChip('chat', { id: 'c1', title: 'Draft' }), null)
  assert.equal(recentsProjectChip('chat', { id: 'c1', title: 'Draft', project: null }), null)
})

test('an app row never yields a chip even if it carries a project', () => {
  assert.equal(recentsProjectChip('app', { id: '9', name: 'App', project: { id: 1, name: 'P' } }), null)
})

test('a project artifact row yields the same project chip as its chat', () => {
  assert.deepEqual(
    recentsProjectChip('artifact', { id: '7:site', name: 'Website', project: { id: 7, name: 'Portfolio' } }),
    { id: '7', name: 'Portfolio', color: null },
  )
})

test('a malformed project (missing id) yields no dead chip', () => {
  assert.equal(recentsProjectChip('chat', { id: 'c1', project: { name: 'Nameless' } }), null)
  assert.equal(recentsProjectChip('chat', { id: 'c1', project: { id: '' } }), null)
})

test('a project without a usable name falls back to "Project"', () => {
  assert.deepEqual(recentsProjectChip('chat', { id: 'c1', project: { id: 3 } }), { id: '3', name: 'Project', color: null })
  assert.deepEqual(recentsProjectChip('chat', { id: 'c1', project: { id: 3, name: '   ' } }), { id: '3', name: 'Project', color: null })
})

test('a project chip carries a normalized color and ignores malformed values', () => {
  assert.deepEqual(
    recentsProjectChip('chat', { project: { id: 3, name: 'Site', color: '#3B82F6' } }),
    { id: '3', name: 'Site', color: '#3b82f6' },
  )
  assert.deepEqual(
    recentsProjectChip('chat', { project: { id: 3, name: 'Site', color: 'blue' } }),
    { id: '3', name: 'Site', color: null },
  )
})
