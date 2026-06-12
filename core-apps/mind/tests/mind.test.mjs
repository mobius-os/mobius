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
  computeRendererFitTransform,
  normalizeRendererGraphData,
  shouldShowScreenLabel,
  renderWikiLinks,
  nodeRadius,
  shouldShowNodeLabel,
  safeMemoryPath,
  neutralizeMemoryMarkdown,
  MEMORY_SANITIZE_OPTIONS,
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
      { id: 'e', title: 'E' },
      { id: 'f', title: 'F' },
    ],
    edges: [
      { source: 'a', target: 'b', kind: 'link' },
      { source: 'b', target: 'c', kind: 'link' },
      { source: 'c', target: 'd', kind: 'link' },
      { source: 'd', target: 'e', kind: 'link' },
      { source: 'e', target: 'f', kind: 'link' },
    ],
  }
  const oneHop = buildLocalGraphData(graph, 'a', 1)
  assert.deepEqual(oneHop.nodes.map((n) => n.id).sort(), ['a', 'b'])
  assert.equal(oneHop.nodes.find((n) => n.id === 'a').localDepth, 0)
  assert.equal(oneHop.nodes.find((n) => n.id === 'b').localDepth, 1)
  assert.equal(oneHop.nodes.every((n) => n.showLabelAlways), true)
  assert.deepEqual(oneHop.links.map((e) => `${e.source}-${e.target}`), ['a-b'])

  const capped = buildLocalGraphData(graph, 'a', 99)
  assert.deepEqual(capped.nodes.map((n) => n.id).sort(), ['a', 'b', 'c', 'd', 'e'])
  assert.equal(capped.links.length, 4)
})

test('screen labels keep global graph selective at low zoom', () => {
  assert.equal(shouldShowScreenLabel({ id: 'hub', type: 'moc' }, 0.2, 99, { mode: 'global' }), true)
  assert.equal(shouldShowScreenLabel({ id: 'plain' }, 0.89, 0, { mode: 'global' }), false)
  assert.equal(shouldShowScreenLabel({ id: 'plain' }, 1.1, 5, { mode: 'global' }), true)
  assert.equal(shouldShowScreenLabel({ id: 'plain' }, 1.1, 6, { mode: 'global' }), false)
})

test('screen labels show local center and nearby nodes before distant nodes', () => {
  assert.equal(shouldShowScreenLabel({ id: 'center', localDepth: 0 }, 0.1, 99, { mode: 'local' }), true)
  assert.equal(shouldShowScreenLabel({ id: 'near', localDepth: 1 }, 0.72, 99, { mode: 'local' }), true)
  assert.equal(shouldShowScreenLabel({ id: 'far', localDepth: 2 }, 1.14, 0, { mode: 'local' }), false)
  assert.equal(shouldShowScreenLabel({ id: 'far', localDepth: 2 }, 1.15, 0, { mode: 'local' }), true)
})

test('normalizeRendererGraphData clones nodes and drops dangling links', () => {
  const out = normalizeRendererGraphData({
    nodes: [
      { id: 'a', title: 'A' },
      { id: 'b', title: 'B', x: 12, y: -3 },
    ],
    links: [
      { source: 'a', target: 'b', kind: 'link' },
      { source: 'a', target: 'missing', kind: 'link' },
    ],
  }, 400, 300)

  assert.equal(out.nodes.length, 2)
  assert.equal(out.links.length, 1)
  assert.equal(out.links[0].source.id, 'a')
  assert.equal(out.links[0].target.id, 'b')
  assert.equal(out.nodes.find((n) => n.id === 'b').x, 12)
  assert.equal(Number.isFinite(out.nodes.find((n) => n.id === 'a').x), true)
})

test('computeRendererFitTransform centers finite graph bounds within limits', () => {
  const fit = computeRendererFitTransform([
    { id: 'a', x: -100, y: -50 },
    { id: 'b', x: 100, y: 50 },
  ], 400, 300, { padding: 40, minScale: 0.5, maxScale: 1.2 })

  assert.equal(fit.k <= 1.2, true)
  assert.equal(fit.k >= 0.5, true)
  assert.equal(Math.round(fit.x), 200)
  assert.equal(Math.round(fit.y), 150)
})

test('safeMemoryPath accepts normal markdown note paths and encodes segments', () => {
  assert.equal(safeMemoryPath('notes/about me.md'), 'notes/about%20me.md')
  assert.equal(safeMemoryPath('mocs/platform.md'), 'mocs/platform.md')
})

test('safeMemoryPath rejects traversal, absolute, empty, and non-markdown paths', () => {
  const bad = [
    null,
    undefined,
    '',
    '   ',
    '/etc/passwd',
    '..\\notes\\x.md',
    'notes/../../service-token.txt',
    'notes/./x.md',
    'notes//x.md',
    'notes/x.md?inline=1',
    'notes/x.md#frag',
    'notes/x.txt',
  ]
  for (const path of bad) {
    assert.equal(safeMemoryPath(path), null, String(path))
  }
})

test('neutralizeMemoryMarkdown keeps labels but removes urls before rendering', () => {
  const md = [
    '![remote pixel](https://example.test/track.png)',
    '[source](https://example.test/page)',
    '[local](notes/idea.md)',
  ].join('\n')
  const out = neutralizeMemoryMarkdown(md)

  assert.ok(out.includes('remote pixel'))
  assert.ok(out.includes('source'))
  assert.ok(out.includes('local'))
  assert.ok(!out.includes('https://'))
  assert.ok(!out.includes('notes/idea.md'))
})

test('neutralizeMemoryMarkdown leaves wikilink syntax for renderWikiLinks', () => {
  const md = 'See [[some-note]] and [[other|alias]] but not [ext](https://evil.test/x).'
  const out = renderWikiLinks(neutralizeMemoryMarkdown(md), [
    { id: 'some-note', title: 'Some Note' },
  ])
  assert.ok(out.includes('[Some Note](#mind-node-some-note)'))
  assert.ok(out.includes('[alias](#mind-node-other)'))
  assert.ok(!out.includes('https://evil.test'))
})

test('memory sanitizer forbids network-bearing tags and attributes', () => {
  assert.ok(MEMORY_SANITIZE_OPTIONS.FORBID_TAGS.includes('img'))
  assert.ok(MEMORY_SANITIZE_OPTIONS.FORBID_TAGS.includes('iframe'))
  assert.ok(MEMORY_SANITIZE_OPTIONS.FORBID_TAGS.includes('form'))
  assert.ok(MEMORY_SANITIZE_OPTIONS.FORBID_ATTR.includes('src'))
  assert.ok(MEMORY_SANITIZE_OPTIONS.FORBID_ATTR.includes('srcset'))
  // href is deliberately NOT forbidden — wikilink anchors need it; the
  // restrictNoteHtml pass strips every non-#mind-node- href instead.
  assert.ok(!MEMORY_SANITIZE_OPTIONS.FORBID_ATTR.includes('href'))
})
