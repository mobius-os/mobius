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
  buildLocalGraphData,
  renderWikiLinks,
  nodeRadius,
  shouldShowNodeLabel,
} = await import('./.build/index.mjs')

test('shouldShowNodeLabel hides ordinary nodes below every threshold except close zoom', () => {
  const node = { id: 'plain', importance: 6, mocs: [] }
  assert.equal(shouldShowNodeLabel(0.9499, node, null), false)
  assert.equal(shouldShowNodeLabel(0.95, node, null), true)
})

test('shouldShowNodeLabel always shows small-graph labels when marked', () => {
  assert.equal(shouldShowNodeLabel(0.001, { id: 'plain', showLabelAlways: true }, null), true)
  assert.equal(shouldShowNodeLabel(undefined, { id: 'plain', showLabelAlways: true }, null), true)
})

test('shouldShowNodeLabel shows MOC-linked nodes at 0.24 and above', () => {
  const node = { id: 'linked', importance: 1, mocs: ['projects'] }
  assert.equal(shouldShowNodeLabel(0.2399, node, null), false)
  assert.equal(shouldShowNodeLabel(0.24, node, null), true)
})

test('shouldShowNodeLabel always shows hovered nodes', () => {
  const node = { id: 'hovered', importance: 1, mocs: [] }
  assert.equal(shouldShowNodeLabel(0.001, node, 'hovered'), true)
})

test('shouldShowNodeLabel always shows MOC and local-center nodes', () => {
  assert.equal(shouldShowNodeLabel(0.001, { id: 'hub', type: 'moc' }, null), true)
  assert.equal(shouldShowNodeLabel(0.001, { id: 'center', localDepth: 0 }, null), true)
})

test('shouldShowNodeLabel shows important nodes at 0.18', () => {
  const important = { id: 'important', importance: 7, mocs: [] }
  const almostImportant = { id: 'almost', importance: 6.99, mocs: [] }
  assert.equal(shouldShowNodeLabel(0.1799, important, null), false)
  assert.equal(shouldShowNodeLabel(0.18, important, null), true)
  assert.equal(shouldShowNodeLabel(0.18, almostImportant, null), false)
})

test('shouldShowNodeLabel rejects malformed scales for threshold labels', () => {
  assert.equal(shouldShowNodeLabel(Number.NaN, { id: 'x', mocs: ['m'] }, null), false)
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

test('renderWikiLinks replaces slugs with note titles and keeps aliases', () => {
  const md = 'See [[abc]] and [[def|custom label]] and [[missing]].'
  const out = renderWikiLinks(md, [
    { id: 'abc', title: 'Alpha Beta' },
    { id: 'def', title: 'Delta Echo' },
  ])
  assert.equal(
    out,
    'See [Alpha Beta](#mind-node-abc) and [custom label](#mind-node-def) and [missing](#mind-node-missing).',
  )
})

test('buildLocalGraphData returns a depth-limited neighborhood', () => {
  const graph = {
    nodes: [
      { id: 'a', title: 'A' },
      { id: 'b', title: 'B' },
      { id: 'c', title: 'C' },
      { id: 'd', title: 'D' },
    ],
    edges: [
      { source: 'a', target: 'b', kind: 'link' },
      { source: 'b', target: 'c', kind: 'link' },
      { source: 'c', target: 'd', kind: 'link' },
    ],
  }
  const oneHop = buildLocalGraphData(graph, 'a', 1)
  assert.deepEqual(oneHop.nodes.map((n) => n.id).sort(), ['a', 'b'])
  assert.equal(oneHop.nodes.find((n) => n.id === 'a').localDepth, 0)
  assert.equal(oneHop.nodes.find((n) => n.id === 'b').localDepth, 1)
  assert.equal(oneHop.nodes.every((n) => n.showLabelAlways), true)
  assert.deepEqual(oneHop.links.map((e) => `${e.source}-${e.target}`), ['a-b'])

  const all = buildLocalGraphData(graph, 'a', -1)
  assert.deepEqual(all.nodes.map((n) => n.id).sort(), ['a', 'b', 'c', 'd'])
  assert.equal(all.links.length, 3)
})
