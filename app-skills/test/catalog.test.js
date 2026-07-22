import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  DEFAULT_SOURCES,
  sourceKey,
  treeToSkills,
  catalogSummary,
  treeScanUrl,
  rawSkillUrl,
  githubSkillUrl,
  createSummaryPrefetcher,
  assessCompat,
  assessInstalled,
} from '../catalog.js'

// Regression tests for the catalog core. Portable: no absolute paths, no
// install, discovered by `node --test` on a fresh clone.

test('DEFAULT_SOURCES: every entry is scannable (repo shape, label, ref)', () => {
  assert.ok(DEFAULT_SOURCES.length >= 5)
  for (const s of DEFAULT_SOURCES) {
    assert.match(s.repo, /^[\w.-]+\/[\w.-]+$/)
    assert.ok(s.label)
    assert.ok(s.ref)
    assert.equal(typeof s.path, 'string')
  }
})

test('sourceKey: distinguishes two subtrees of the same repo', () => {
  const bundled = { repo: 'NousResearch/hermes-agent', path: 'skills' }
  const optional = { repo: 'NousResearch/hermes-agent', path: 'optional-skills' }
  assert.notEqual(sourceKey(bundled), sourceKey(optional))
})

test('treeToSkills: keeps only SKILL.md dirs, sorted by name', () => {
  const tree = [
    { path: 'skills/pdf/SKILL.md' },
    { path: 'skills/artifacts/SKILL.md' },
    { path: 'skills/pdf/scripts/fill.py' },
    { path: 'README.md' },
    { path: 'skills/notes.md' },
  ]
  assert.deepEqual(treeToSkills(tree, ''), [
    { dir: 'skills/artifacts', name: 'artifacts' },
    { dir: 'skills/pdf', name: 'pdf' },
  ])
})

test('treeToSkills: a path prefix scopes to that subtree (boundary-safe)', () => {
  const tree = [
    { path: 'skills/a/SKILL.md' },
    { path: 'skills-extra/b/SKILL.md' },
    { path: 'other/c/SKILL.md' },
  ]
  assert.deepEqual(treeToSkills(tree, 'skills'), [{ dir: 'skills/a', name: 'a' }])
})

test('treeToSkills: tolerates malformed tree entries and a non-array input', () => {
  assert.deepEqual(treeToSkills(null, ''), [])
  assert.deepEqual(treeToSkills([{}, { path: 42 }, null, { path: 'x/SKILL.md' }], ''), [
    { dir: 'x', name: 'x' },
  ])
})

test('catalogSummary: frontmatter description wins; license and peek extracted', () => {
  const md = [
    '---',
    'name: pdf',
    'description: Work with PDF files.',
    'license: Complete terms in LICENSE.txt',
    '---',
    '# PDF',
    '',
    'Body paragraph here.',
  ].join('\n')
  const s = catalogSummary(md)
  assert.equal(s.description, 'Work with PDF files.')
  assert.equal(s.license, 'Complete terms in LICENSE.txt')
  assert.ok(s.peek.startsWith('# PDF'))
  assert.ok(!s.peek.includes('---\nname'), 'peek is the frontmatter-stripped body')
})

test('catalogSummary: falls back to the first body paragraph, then placeholder copy', () => {
  assert.equal(catalogSummary('# T\n\nFirst paragraph.').description, 'First paragraph.')
  assert.equal(catalogSummary('').description, 'No description in SKILL.md.')
  assert.equal(catalogSummary('').peek, null)
})

test('catalogSummary: peek is capped at 700 chars', () => {
  const s = catalogSummary(`# T\n\n${'x'.repeat(2000)}`)
  assert.equal(s.peek.length, 700)
})

test('url builders: ref defaults to main and paths compose correctly', () => {
  const src = { repo: 'anthropics/skills', path: 'skills' }
  assert.equal(treeScanUrl(src), 'https://api.github.com/repos/anthropics/skills/git/trees/main?recursive=1')
  assert.equal(rawSkillUrl(src, 'skills/pdf'), 'https://raw.githubusercontent.com/anthropics/skills/main/skills/pdf/SKILL.md')
  assert.equal(githubSkillUrl({ ...src, ref: 'v2' }, 'skills/pdf'), 'https://github.com/anthropics/skills/blob/v2/skills/pdf/SKILL.md')
})

test('prefetcher: bounds concurrency and still visits every dir', async () => {
  let inflight = 0
  let peak = 0
  const seen = []
  const prefetcher = createSummaryPrefetcher({
    concurrency: 2,
    loadOne: async (dir) => {
      inflight += 1
      peak = Math.max(peak, inflight)
      seen.push(dir)
      await new Promise((resolve) => setImmediate(resolve))
      inflight -= 1
    },
  })
  prefetcher.start(['a', 'b', 'c', 'd', 'e'])
  await new Promise((resolve) => setTimeout(resolve, 50))
  assert.deepEqual([...seen].sort(), ['a', 'b', 'c', 'd', 'e'])
  assert.ok(peak <= 2, `peak concurrency ${peak} exceeded the bound`)
})

test('prefetcher: a rejecting loadOne does not stall the pool', async () => {
  const seen = []
  const prefetcher = createSummaryPrefetcher({
    concurrency: 1,
    loadOne: async (dir) => {
      seen.push(dir)
      if (dir === 'bad') throw new Error('boom')
    },
  })
  prefetcher.start(['bad', 'good'])
  await new Promise((resolve) => setTimeout(resolve, 20))
  assert.deepEqual(seen, ['bad', 'good'])
})

test('prefetcher: starting a new pool strands the previous generation', async () => {
  const seen = []
  let releaseFirst
  const gate = new Promise((resolve) => { releaseFirst = resolve })
  const prefetcher = createSummaryPrefetcher({
    concurrency: 1,
    loadOne: async (dir) => {
      seen.push(dir)
      if (dir === 'old-1') await gate // old pool blocks until after the switch
    },
  })
  prefetcher.start(['old-1', 'old-2'])
  await new Promise((resolve) => setImmediate(resolve))
  prefetcher.start(['new-1'])
  releaseFirst()
  await new Promise((resolve) => setTimeout(resolve, 20))
  assert.ok(seen.includes('new-1'))
  assert.ok(!seen.includes('old-2'), 'the superseded pool must not continue its queue')
})

test('prefetcher: cancel() stops the pool without starting another', async () => {
  const seen = []
  let release
  const gate = new Promise((resolve) => { release = resolve })
  const prefetcher = createSummaryPrefetcher({
    concurrency: 1,
    loadOne: async (dir) => {
      seen.push(dir)
      if (dir === 'a') await gate
    },
  })
  prefetcher.start(['a', 'b'])
  await new Promise((resolve) => setImmediate(resolve))
  prefetcher.cancel()
  release()
  await new Promise((resolve) => setTimeout(resolve, 20))
  assert.deepEqual(seen, ['a'])
})

// --- assessCompat: the pre-install badge's prediction of the installer ---

const blob = (path, size = 100) => ({ path, type: 'blob', size })
const DIR = 'skills/pdf'
const OK_MD = '---\nname: pdf\ndescription: Fill and read PDFs.\n---\n\nProse body.\n'

test('assessCompat: clean prose skill is ok', () => {
  const tree = [blob(`${DIR}/SKILL.md`), blob(`${DIR}/references/forms.md`)]
  const res = assessCompat(tree, DIR, OK_MD)
  assert.equal(res.ok, true)
  assert.deepEqual(res.caveats, [])
})

test('assessCompat: disallowed extensions and deep nesting are flagged as dropped', () => {
  const tree = [
    blob(`${DIR}/SKILL.md`),
    blob(`${DIR}/binary.wasm`),
    blob(`${DIR}/a/b/c/d/e/deep.md`),
  ]
  const res = assessCompat(tree, DIR, OK_MD)
  const dropped = res.caveats.find((c) => c.kind === 'dropped')
  assert.ok(dropped)
  assert.match(dropped.text, /2 extra files/)
  assert.match(dropped.text, /binary\.wasm/)
})

test('assessCompat: over the file-count budget → installs partially', () => {
  const tree = [blob(`${DIR}/SKILL.md`)]
  for (let i = 0; i < 30; i++) tree.push(blob(`${DIR}/ref-${i}.md`))
  const res = assessCompat(tree, DIR, OK_MD)
  const over = res.caveats.find((c) => c.kind === 'over-budget')
  assert.ok(over)
  assert.match(over.text, /30 files \(max 24\)/)
})

test('assessCompat: over the total-size budget → installs partially', () => {
  const tree = [blob(`${DIR}/SKILL.md`), blob(`${DIR}/big.csv`, 3 * 1024 * 1024)]
  const res = assessCompat(tree, DIR, OK_MD)
  const over = res.caveats.find((c) => c.kind === 'over-budget')
  assert.ok(over)
  assert.match(over.text, /max 2 MB/)
})

test('assessCompat: bundled scripts are an informational caveat', () => {
  const tree = [blob(`${DIR}/SKILL.md`), blob(`${DIR}/scripts/fill.py`)]
  const res = assessCompat(tree, DIR, OK_MD)
  assert.equal(res.ok, false)
  const scripts = res.caveats.find((c) => c.kind === 'scripts')
  assert.match(scripts.text, /nothing runs automatically/)
})

test('assessCompat: missing frontmatter description is flagged', () => {
  const tree = [blob(`${DIR}/SKILL.md`)]
  const res = assessCompat(tree, DIR, '# PDF skill\n\nJust a body.\n')
  const fm = res.caveats.find((c) => c.kind === 'frontmatter')
  assert.ok(fm)
})

test('assessCompat: multi-line YAML description defeats the flat parser → flagged', () => {
  const raw = '---\nname: pdf\ndescription: >\n  Long folded\n  description.\n---\n\nBody.\n'
  const res = assessCompat([blob(`${DIR}/SKILL.md`)], DIR, raw)
  assert.ok(res.caveats.find((c) => c.kind === 'frontmatter'))
})

test('assessCompat: refs to dropped or absent files are the broken-refs caveat', () => {
  const tree = [blob(`${DIR}/SKILL.md`), blob(`${DIR}/helper.rb`)]
  const raw = `${OK_MD}\nRun [the helper](helper.rb), read \`scripts/gone.py\`, see [docs](https://example.com/x.md).\n`
  const res = assessCompat(tree, DIR, raw)
  const broken = res.caveats.find((c) => c.kind === 'broken-refs')
  assert.ok(broken)
  assert.match(broken.text, /helper\.rb/)
  assert.match(broken.text, /scripts\/gone\.py/)
  assert.ok(!broken.text.includes('example.com'))
})

test('assessCompat: bare inline-code filenames and dir refs are not treated as refs', () => {
  const tree = [blob(`${DIR}/SKILL.md`)]
  const raw = `${OK_MD}\nMention \`package.json\` and [the scripts](scripts/) generically.\n`
  const res = assessCompat(tree, DIR, raw)
  assert.equal(res.caveats.find((c) => c.kind === 'broken-refs'), undefined)
})

test('assessCompat: shell snippets and home/abs paths in inline code are not refs', () => {
  const tree = [blob(`${DIR}/SKILL.md`)]
  const raw = `${OK_MD}\nRun \`open /tmp/review_<name>.html\`, save to \`~/Downloads/set.json\`, read \`/etc/hosts.conf\`.\n`
  const res = assessCompat(tree, DIR, raw)
  assert.equal(res.caveats.find((c) => c.kind === 'broken-refs'), undefined)
})

// --- assessInstalled: the same verdict for skills already on disk ---

test('assessInstalled: clean installed skill with its files present is ok', () => {
  const raw = `${OK_MD}\nSee [the forms guide](references/forms.md).\n`
  const res = assessInstalled(['references/forms.md'], raw)
  assert.equal(res.ok, true)
  assert.deepEqual(res.caveats, [])
})

test('assessInstalled: refs to files not on disk are broken-refs', () => {
  const raw = `${OK_MD}\nRun \`scripts/fill.py\` first.\n`
  const res = assessInstalled([], raw)
  const broken = res.caveats.find((c) => c.kind === 'broken-refs')
  assert.ok(broken)
  assert.match(broken.text, /scripts\/fill\.py/)
})

test('assessInstalled: installed scripts are the informational caveat', () => {
  const res = assessInstalled(['scripts/fill.py'], `${OK_MD}\nRun \`scripts/fill.py\`.\n`)
  assert.equal(res.ok, false)
  const scripts = res.caveats.find((c) => c.kind === 'scripts')
  assert.match(scripts.text, /nothing runs automatically/)
  assert.equal(res.caveats.find((c) => c.kind === 'broken-refs'), undefined)
})

test('assessInstalled: flat skill (no files) with plain prose and a description is ok', () => {
  assert.equal(assessInstalled([], OK_MD).ok, true)
})

test('assessInstalled: missing frontmatter description is flagged', () => {
  const res = assessInstalled([], '# Notes\n\nJust a body.\n')
  assert.ok(res.caveats.find((c) => c.kind === 'frontmatter'))
})

test('assessInstalled: SKILL.md itself never counts as a resource or a broken ref', () => {
  const res = assessInstalled(['SKILL.md'], `${OK_MD}\nSee [itself](SKILL.md).\n`)
  assert.equal(res.ok, true)
})

test('assessCompat: files outside the skill dir are ignored', () => {
  const tree = [
    blob(`${DIR}/SKILL.md`),
    blob('skills/other/huge.bin', 10 * 1024 * 1024),
    blob('README.rb'),
  ]
  const res = assessCompat(tree, DIR, OK_MD)
  assert.equal(res.ok, true)
})
