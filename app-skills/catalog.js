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
