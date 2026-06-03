import { test } from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdirSync } from 'node:fs'

const esbuild = '/home/hmzmrzx/projects/mobius/frontend/node_modules/.bin/esbuild'
const nodePath = '/home/hmzmrzx/projects/mobius/frontend/node_modules'
mkdirSync(new URL('./.build/', import.meta.url), { recursive: true })
execFileSync(esbuild, [
  '--bundle',
  '--format=esm',
  '--jsx=automatic',
  '--platform=node',
  'index.jsx',
  '--outfile=tests/.build/index.mjs',
], {
  cwd: new URL('..', import.meta.url),
  env: { ...process.env, NODE_PATH: nodePath },
  stdio: 'pipe',
})

const {
  nodeRadius,
  shouldShowNodeLabel,
} = await import('./.build/index.mjs')

test('shouldShowNodeLabel hides ordinary nodes below every threshold except broad zoom', () => {
  const node = { id: 'plain', importance: 6, mocs: [] }
  assert.equal(shouldShowNodeLabel(0.3999, node, null), false)
  assert.equal(shouldShowNodeLabel(0.4, node, null), false)
  assert.equal(shouldShowNodeLabel(0.8, node, null), false)
  assert.equal(shouldShowNodeLabel(1.3999, node, null), false)
  assert.equal(shouldShowNodeLabel(1.4, node, null), true)
})

test('shouldShowNodeLabel shows MOC-linked nodes at 0.4 and above', () => {
  const node = { id: 'linked', importance: 1, mocs: ['projects'] }
  assert.equal(shouldShowNodeLabel(0.3999, node, null), false)
  assert.equal(shouldShowNodeLabel(0.4, node, null), true)
  assert.equal(shouldShowNodeLabel(0.8, node, null), true)
})

test('shouldShowNodeLabel shows hovered nodes at 0.4 and above', () => {
  const node = { id: 'hovered', importance: 1, mocs: [] }
  assert.equal(shouldShowNodeLabel(0.3999, node, 'hovered'), false)
  assert.equal(shouldShowNodeLabel(0.4, node, 'hovered'), true)
  assert.equal(shouldShowNodeLabel(0.8, node, 'hovered'), true)
})

test('shouldShowNodeLabel shows important nodes at 0.8 but not 0.4', () => {
  const important = { id: 'important', importance: 7, mocs: [] }
  const almostImportant = { id: 'almost', importance: 6.99, mocs: [] }
  assert.equal(shouldShowNodeLabel(0.4, important, null), false)
  assert.equal(shouldShowNodeLabel(0.7999, important, null), false)
  assert.equal(shouldShowNodeLabel(0.8, important, null), true)
  assert.equal(shouldShowNodeLabel(0.8, almostImportant, null), false)
})

test('shouldShowNodeLabel rejects malformed scales', () => {
  assert.equal(shouldShowNodeLabel(Number.NaN, { id: 'x', mocs: ['m'] }, 'x'), false)
  assert.equal(shouldShowNodeLabel(Infinity, { id: 'x' }, null), false)
})

test('nodeRadius uses importance and access count for ordinary nodes', () => {
  assert.equal(nodeRadius({ importance: 1, access_count: 0 }), 4.55)
  assert.equal(nodeRadius({ importance: 5, access_count: 0 }), 10.75)
  assert.equal(nodeRadius({ importance: 1, access_count: 7 }), 9.2)
})

test('nodeRadius applies the MOC multiplier', () => {
  assert.equal(nodeRadius({ type: 'moc', importance: 5, access_count: 0 }), 15.049999999999999)
})

test('nodeRadius guards sparse and malformed node data', () => {
  assert.equal(nodeRadius(), 4.55)
  assert.equal(nodeRadius({ importance: -5, access_count: -2 }), 4.55)
  assert.equal(nodeRadius({ importance: Number.NaN, access_count: Infinity }), 4.55)
})
