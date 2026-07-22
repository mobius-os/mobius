// Dependency-free core for the catalog screen — the curated source list,
// git-trees scan filtering, SKILL.md summary parsing, and the background
// summary prefetch pool. No React and no direct network: fetching is injected
// by index.jsx (which routes it through /api/proxy), so everything here is
// unit-testable (see test/catalog.test.js).

import { parseSkill, splitFrontmatter } from './domain.js'

// Verified catalogs that HOST SKILL.md-format skills — link-list "awesome"
// repos don't render here (nothing installable to scan); hand those to the
// agent instead. `path` scopes the tree scan to a subtree; '' scans the whole
// repo. The list is app data too: a sources.json in app storage overrides it,
// so "add this repo as a source" is a chat request, not a code change.
export const DEFAULT_SOURCES = [
  { label: 'Anthropic Skills', repo: 'anthropics/skills', path: 'skills', ref: 'main',
    blurb: 'Official Anthropic skills — documents, artifacts, MCP building, testing.' },
  { label: 'Anthropic Knowledge Work', repo: 'anthropics/knowledge-work-plugins', path: '', ref: 'main',
    blurb: 'Anthropic’s knowledge-worker plugins — research, bio, finance, legal, and more.' },
  { label: 'Superpowers', repo: 'obra/superpowers', path: 'skills', ref: 'main',
    blurb: 'The famous dev-methodology set — brainstorming, planning, TDD, debugging.' },
  { label: 'Trail of Bits Security', repo: 'trailofbits/skills', path: '', ref: 'main',
    blurb: 'Security research, vulnerability detection, and audit workflows.' },
  { label: 'Cloudflare', repo: 'cloudflare/skills', path: 'skills', ref: 'main',
    blurb: 'Official Cloudflare skills for building on Workers and the CF platform.' },
  { label: 'Hermes bundled', repo: 'NousResearch/hermes-agent', path: 'skills', ref: 'main',
    blurb: 'Nous Research’s always-on Hermes agent skills.' },
  { label: 'Hermes optional', repo: 'NousResearch/hermes-agent', path: 'optional-skills', ref: 'main',
    blurb: 'The big Hermes catalog — blockchain, research, media, agents, and more.' },
]

export function sourceKey(source) {
  return `${source?.repo || ''}/${source?.path || ''}`
}

// One recursive git-trees call finds every SKILL.md in the repo — flat cards,
// no folder drilling, no dead ends. This filters the raw tree down to skill
// directories under the source's path prefix.
export function treeToSkills(tree, pathPrefix) {
  const prefix = String(pathPrefix || '').replace(/^\/+|\/+$/g, '')
  const entries = Array.isArray(tree) ? tree : []
  return entries
    .filter((t) => typeof t?.path === 'string' && t.path.endsWith('/SKILL.md'))
    .map((t) => t.path.slice(0, -'/SKILL.md'.length))
    .filter((dir) => !prefix || dir === prefix || dir.startsWith(`${prefix}/`))
    .map((dir) => ({ dir, name: dir.split('/').pop() }))
    .sort((a, b) => a.name.localeCompare(b.name))
}

// A raw SKILL.md → what the card shows. Frontmatter `description` is the
// ecosystem's "when to use this" line, so it wins over the first body
// paragraph; parseSkill supplies the fence-aware fallback and the
// frontmatter-stripped body for the peek.
export function catalogSummary(text) {
  const { meta } = splitFrontmatter(text || '')
  const parsed = parseSkill('SKILL.md', text || '')
  return {
    description: meta.description || parsed.description || 'No description in SKILL.md.',
    license: meta.license || null,
    peek: (parsed.content || '').trim().slice(0, 700) || null,
  }
}

// Mirror of the backend's install bounds (backend/app/routes/skills.py —
// _RESOURCE_COUNT_MAX / _RESOURCE_TOTAL_MAX / _RESOURCE_MAX_DEPTH /
// _RESOURCE_SUFFIXES). Advisory display only: the backend enforces for real,
// this just predicts what it will do so the badge can warn before install.
export const INSTALL_LIMITS = {
  maxFiles: 24,
  maxTotalBytes: 2 * 1024 * 1024,
  maxDepth: 4,
  suffixes: ['.md', '.txt', '.json', '.yaml', '.yml', '.csv', '.py', '.js', '.ts',
    '.sh', '.toml', '.html', '.css'],
}

const SCRIPT_SUFFIXES = ['.py', '.js', '.ts', '.sh']

function suffixOf(path) {
  const base = path.split('/').pop() || ''
  const dot = base.lastIndexOf('.')
  return dot > 0 ? base.slice(dot).toLowerCase() : ''
}

// Relative paths mentioned in SKILL.md — markdown links/images plus bare
// inline-code paths like `scripts/helper.py`. External URLs and anchors are
// not the skill's files, so they're skipped.
export function relativeRefs(raw) {
  const refs = new Set()
  const consider = (target, { needsSlash } = {}) => {
    const t = String(target || '').trim().replace(/^\.\//, '').split(/[#?]/)[0]
    if (!t || t.startsWith('/') || t.startsWith('~') || t.includes('..') || /^[a-z][a-z0-9+.-]*:/i.test(t)) return
    // A shell snippet (`open /tmp/x.html`) or home path is not a bundled file.
    if (/\s/.test(t)) return
    if (needsSlash && !t.includes('/')) return
    // Files only — a trailing dir ref like `scripts/` isn't checkable.
    if (/\.[a-z0-9]{1,6}$/i.test(t)) refs.add(t)
  }
  for (const m of String(raw || '').matchAll(/!?\[[^\]]*\]\(([^)\s]+)[^)]*\)/g)) consider(m[1])
  // Inline code must be path-shaped (`scripts/helper.py`) — a bare `foo.json`
  // is usually a generic mention, not a bundled file.
  for (const m of String(raw || '').matchAll(/`([^`\n]+\.[a-z0-9]{1,5})`/gi)) consider(m[1], { needsSlash: true })
  return [...refs]
}

// Predict how POST /api/skills/install would treat a catalog skill, from data
// the screen already holds: the source's recursive git tree and the raw
// SKILL.md. Returns { ok, caveats: [{ kind, text }] } — ok means "installs
// whole and indexes cleanly", caveats are ordered most→least serious.
export function assessCompat(tree, dir, raw) {
  const caveats = []
  const prefix = `${String(dir || '').replace(/\/+$/g, '')}/`
  const files = (Array.isArray(tree) ? tree : [])
    .filter((t) => t?.type === 'blob' && typeof t?.path === 'string' && t.path.startsWith(prefix))
    .map((t) => ({ rel: t.path.slice(prefix.length), size: Number(t.size) || 0 }))

  const kept = []
  const dropped = []
  for (const f of files) {
    if (/^skill\.md$/i.test(f.rel)) continue
    const depthOk = f.rel.split('/').length - 1 <= INSTALL_LIMITS.maxDepth
    const suffixOk = INSTALL_LIMITS.suffixes.includes(suffixOf(f.rel))
    ;(depthOk && suffixOk ? kept : dropped).push(f)
  }

  // Install materializes resources in order and stops adding once over budget;
  // predicting the exact survivors would overfit, so over-budget is its own
  // "installs partially" caveat instead.
  const total = kept.reduce((n, f) => n + f.size, 0)
  const overCount = kept.length > INSTALL_LIMITS.maxFiles
  const overSize = total > INSTALL_LIMITS.maxTotalBytes

  // A ref is broken when it names a file the install will drop, or a file the
  // tree scan proves doesn't exist in the skill dir at all.
  const keptSet = new Set(kept.map((f) => f.rel))
  const brokenRefs = relativeRefs(raw).filter(
    (r) => !keptSet.has(r) && !/^skill\.md$/i.test(r),
  )
  if (brokenRefs.length) {
    caveats.push({
      kind: 'broken-refs',
      text: `Its instructions mention files that won't be there after install (${nameSome(brokenRefs)}), so the steps that use them may not work.`,
    })
  }
  if (dropped.length) {
    caveats.push({
      kind: 'dropped',
      text: `${dropped.length} extra ${dropped.length === 1 ? 'file' : 'files'} won't be copied — Möbius only installs common text files, and ${dropped.length === 1 ? 'this one is' : 'these are'} a different type or buried too deep: ${nameSome(dropped.map((f) => f.rel))}. The main instructions still install fine.`,
    })
  }
  if (overCount || overSize) {
    const parts = []
    if (overCount) parts.push(`${kept.length} files (max ${INSTALL_LIMITS.maxFiles})`)
    if (overSize) parts.push(`${(total / (1024 * 1024)).toFixed(1)} MB (max ${INSTALL_LIMITS.maxTotalBytes / (1024 * 1024)} MB)`)
    caveats.push({
      kind: 'over-budget',
      text: `This skill is bigger than Möbius's install limit — ${parts.join(', ')} — so only part of it will be copied.`,
    })
  }

  const scripts = kept.filter((f) => SCRIPT_SUFFIXES.includes(suffixOf(f.rel)))
  if (scripts.length) {
    caveats.push({
      kind: 'scripts',
      text: `Comes with ${scripts.length} helper ${scripts.length === 1 ? 'script' : 'scripts'}. Möbius saves them for the agent to read — nothing runs automatically.`,
    })
  }

  const fm = frontmatterCaveat(raw)
  if (fm) caveats.push(fm)

  return { ok: caveats.length === 0, caveats }
}

// Both flat parsers (here and backend) read only `key: value` scalars, so a
// YAML block scalar (`description: >`) leaves just the indicator behind.
function frontmatterCaveat(raw) {
  const desc = String(splitFrontmatter(raw || '').meta.description || '').trim()
  if (desc && !/^[>|][+-]?$/.test(desc)) return null
  return {
    kind: 'frontmatter',
    text: 'Missing its one-line summary, so skill lists will show its first paragraph instead. Purely cosmetic.',
  }
}

// The same verdict for an already-INSTALLED skill, from what's actually on
// disk: `files` is the installed resource list relative to the skill dir
// (empty for flat skills). The repo-side caveats (dropped, over-budget) are
// install-time facts we can no longer see — their lasting symptom is a
// reference to a file that isn't there, which this does catch.
export function assessInstalled(files, raw) {
  const caveats = []
  const rels = (Array.isArray(files) ? files : [])
    .map((f) => String(f || ''))
    .filter((r) => r && !/^skill\.md$/i.test(r))
  const have = new Set(rels)

  const broken = relativeRefs(raw).filter((r) => !have.has(r) && !/^skill\.md$/i.test(r))
  if (broken.length) {
    caveats.push({
      kind: 'broken-refs',
      text: `Its instructions mention files that aren't installed (${nameSome(broken)}), so the steps that use them may not work.`,
    })
  }

  const scripts = rels.filter((r) => SCRIPT_SUFFIXES.includes(suffixOf(r)))
  if (scripts.length) {
    caveats.push({
      kind: 'scripts',
      text: `Comes with ${scripts.length} helper ${scripts.length === 1 ? 'script' : 'scripts'}. Möbius saves them for the agent to read — nothing runs automatically.`,
    })
  }

  const fm = frontmatterCaveat(raw)
  if (fm) caveats.push(fm)

  return { ok: caveats.length === 0, caveats }
}

function nameSome(names, cap = 4) {
  const shown = names.slice(0, cap).join(', ')
  return names.length > cap ? `${shown}, +${names.length - cap} more` : shown
}

export function treeScanUrl(source) {
  return `https://api.github.com/repos/${source.repo}/git/trees/${source.ref || 'main'}?recursive=1`
}

export function rawSkillUrl(source, dir) {
  return `https://raw.githubusercontent.com/${source.repo}/${source.ref || 'main'}/${dir}/SKILL.md`
}

export function githubSkillUrl(source, dir) {
  return `https://github.com/${source.repo}/blob/${source.ref || 'main'}/${dir}/SKILL.md`
}

// Background prefetch pool: after a scan, walk every dir through `loadOne`
// with bounded concurrency so all summaries are loaded before the owner
// scrolls to them (raw-file fetches — no GitHub API rate cost). start()
// supersedes any previous pool via a generation counter, so switching sources
// mid-prefetch strands the stale workers instead of racing them; cancel()
// stops without starting a new pool. `loadOne` must dedupe by dir itself —
// viewport-priority loads from the cards may race the pool.
export function createSummaryPrefetcher({ loadOne, concurrency = 5 }) {
  let generation = 0

  function start(dirs) {
    const gen = ++generation
    const queue = Array.isArray(dirs) ? [...dirs] : []
    let next = 0
    const worker = () => {
      if (gen !== generation) return
      const dir = queue[next++]
      if (dir === undefined) return
      Promise.resolve()
        .then(() => loadOne(dir))
        .then(worker, worker)
    }
    const workers = Math.min(concurrency, queue.length)
    for (let k = 0; k < workers; k++) worker()
  }

  function cancel() {
    generation += 1
  }

  return { start, cancel }
}
