import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  parseSkill,
  classifyLink,
  selectSystemPromptApps,
  fetchSystemPromptApps,
  createSystemPromptAppsLoader,
  installedAppDisplayName,
  friendlyLoadError,
  skillContentPath,
  provenanceChip,
  isUninstallable,
  skillDisplayTitle,
  usageLabel,
} from '../domain.js'

// Regression tests for the dependency-free core. Portable: no absolute paths,
// no install, discovered by `node --test` on a fresh clone.

test('parseSkill: title from first heading, description from first paragraph', () => {
  const s = parseSkill('building-apps.md', '# Building mini-apps\n\nThe full mini-app contract.\n\nMore text here.')
  assert.equal(s.slug, 'building-apps')
  assert.equal(s.name, 'building-apps.md')
  assert.equal(s.title, 'Building mini-apps')
  assert.equal(s.description, 'The full mini-app contract.')
  assert.equal(s.content, '# Building mini-apps\n\nThe full mini-app contract.\n\nMore text here.')
})

test('parseSkill: falls back to Title-Cased slug when no heading', () => {
  const s = parseSkill('cron-jobs.md', 'no heading here, just prose')
  assert.equal(s.title, 'Cron Jobs')
})

test('parseSkill: a "# comment" inside a fenced code block is not the title', () => {
  const md = '```bash\n# not a title\necho hi\n```\n\n# Real Title\n\nReal description.'
  const s = parseSkill('x.md', md)
  assert.equal(s.title, 'Real Title')
  assert.equal(s.description, 'Real description.')
})

test('parseSkill: description skips a fenced block and stops at the next heading', () => {
  const md = '# T\n\nFirst para.\n\n## Section\n\nSecond para.'
  const s = parseSkill('x.md', md)
  assert.equal(s.description, 'First para.')
})

test('parseSkill: empty content still yields a slug-derived title and empty description', () => {
  const s = parseSkill('theming.md', '')
  assert.equal(s.title, 'Theming')
  assert.equal(s.description, '')
})

test('parseSkill: strips frontmatter from rendered content', () => {
  const md = [
    '---',
    'name: impeccable',
    'description: Improve frontend interfaces.',
    '---',
    '# impeccable',
    '',
    'Use this skill for product UI polish.',
  ].join('\n')
  const s = parseSkill('impeccable.md', md)
  assert.equal(s.title, 'impeccable')
  assert.equal(s.description, 'Use this skill for product UI polish.')
  assert.equal(s.content, '# impeccable\n\nUse this skill for product UI polish.')
})

test('parseSkill: uses frontmatter description as fallback only when body has none', () => {
  const s = parseSkill('quiet-mode.md', '---\nname: quiet-mode\ndescription: Calm the interface.\n---\n# Quiet Mode')
  assert.equal(s.title, 'Quiet Mode')
  assert.equal(s.description, 'Calm the interface.')
})

test('classifyLink: same-folder .md link resolves to a skill slug', () => {
  assert.deepEqual(classifyLink('app-component-shapes.md'), { kind: 'skill', slug: 'app-component-shapes' })
})

test('classifyLink: ./-prefixed and query/hash-suffixed .md still resolve', () => {
  assert.deepEqual(classifyLink('./notifications.md'), { kind: 'skill', slug: 'notifications' })
  assert.deepEqual(classifyLink('cron.md#schedule'), { kind: 'skill', slug: 'cron' })
})

test('classifyLink: http/https open externally', () => {
  assert.deepEqual(classifyLink('https://example.com/x'), { kind: 'external', url: 'https://example.com/x' })
  assert.deepEqual(classifyLink('http://example.com'), { kind: 'external', url: 'http://example.com' })
})

test('classifyLink: unsupported protocols are blocked', () => {
  assert.equal(classifyLink('mailto:a@b.com').kind, 'blocked')
  assert.equal(classifyLink('javascript:alert(1)').kind, 'blocked')
  assert.equal(classifyLink('file:///etc/passwd').kind, 'blocked')
})

test('classifyLink: sub-path relative links are blocked (no in-app target)', () => {
  assert.equal(classifyLink('sub/dir/other.md').kind, 'blocked')
  assert.equal(classifyLink('../up.md').kind, 'blocked')
})

test('classifyLink: in-page fragments are anchors (harmless)', () => {
  assert.deepEqual(classifyLink('#section'), { kind: 'anchor' })
})

test('classifyLink: empty/missing href is blocked, never navigated', () => {
  assert.equal(classifyLink('').kind, 'blocked')
  assert.equal(classifyLink(null).kind, 'blocked')
  assert.equal(classifyLink(undefined).kind, 'blocked')
})

test('friendlyLoadError: network failures become actionable copy', () => {
  assert.match(friendlyLoadError(new Error('Failed to fetch')), /connection/i)
  assert.match(friendlyLoadError(new Error('list 500')), /error/i)
})

test('selectSystemPromptApps: keeps only true system apps with a prompt file and sorts by name', () => {
  const apps = [
    { id: 4, name: 'Memory', system_app: true, system_prompt_file: 'memory-core.md' },
    { id: 2, name: 'Artifacts', system_app: true, system_prompt_file: 'artifacts-core.md' },
    { id: 1, name: 'Skills', system_app: false, system_prompt_file: 'skills-core.md' },
    { id: 3, name: 'Legacy', system_app: true, system_prompt_file: null },
    { id: 6, name: 'Blank', system_app: true, system_prompt_file: '' },
    { id: 5, name: 'Truthy only', system_app: 1, system_prompt_file: 'truthy-core.md' },
  ]

  assert.deepEqual(selectSystemPromptApps(apps).map((app) => app.name), ['Artifacts', 'Memory'])
})

test('selectSystemPromptApps: malformed API payloads are safely empty', () => {
  assert.deepEqual(selectSystemPromptApps(null), [])
  assert.deepEqual(selectSystemPromptApps({ apps: [] }), [])
})

test('fetchSystemPromptApps: HTTP, fetch, and malformed JSON failures degrade to an empty section', async () => {
  const cases = [
    async () => ({ ok: false, status: 500 }),
    async () => { throw new Error('network down') },
    async () => ({ ok: true, json: async () => { throw new SyntaxError('bad JSON') } }),
    async () => ({ ok: true, json: async () => ({ apps: [] }) }),
  ]

  for (const fetchImpl of cases) {
    assert.deepEqual(await fetchSystemPromptApps(fetchImpl, { Authorization: 'Bearer test' }), [])
  }
})

test('fetchSystemPromptApps: an empty apps response omits the supplemental section', async () => {
  const fetchImpl = async () => ({ ok: true, json: async () => [] })
  assert.deepEqual(await fetchSystemPromptApps(fetchImpl, {}), [])
})

test('system prompt apps loader: a slow apps response cannot block the primary skills flow', async () => {
  let resolveApps
  const pendingApps = new Promise((resolve) => { resolveApps = resolve })
  const committed = []
  const loader = createSystemPromptAppsLoader({
    fetchImpl: async () => ({ ok: true, json: () => pendingApps }),
    onApps: (apps) => committed.push(apps),
  })

  const startResult = loader.load({})
  const skills = await Promise.resolve(['Rendered skill'])
  assert.equal(startResult, undefined, 'supplemental load is intentionally fire-and-forget')
  assert.deepEqual(skills, ['Rendered skill'])
  assert.deepEqual(committed, [], 'the apps request is still pending')

  resolveApps([])
  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual(committed, [[]])
})

test('system prompt apps loader: only the latest overlapping request may commit', async () => {
  let resolveOlder
  let resolveNewer
  const older = new Promise((resolve) => { resolveOlder = resolve })
  const newer = new Promise((resolve) => { resolveNewer = resolve })
  let request = 0
  const committed = []
  const loader = createSystemPromptAppsLoader({
    fetchImpl: async () => ({ ok: true, json: () => (request++ === 0 ? older : newer) }),
    onApps: (apps) => committed.push(apps.map((app) => app.name)),
  })

  loader.load({})
  loader.load({})
  resolveNewer([{ id: 2, name: 'Newer', system_app: true, system_prompt_file: 'newer.md' }])
  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual(committed, [['Newer']])

  resolveOlder([{ id: 1, name: 'Older', system_app: true, system_prompt_file: 'older.md' }])
  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual(committed, [['Newer']], 'the stale response cannot overwrite the latest state')
})

test('installedAppDisplayName: trims name, then falls back to slug and neutral copy', () => {
  assert.equal(installedAppDisplayName({ name: '  Memory  ', slug: 'memory' }), 'Memory')
  assert.equal(installedAppDisplayName({ name: '   ', slug: '  legacy-app  ' }), 'legacy-app')
  assert.equal(installedAppDisplayName({ name: '', slug: '' }), 'Installed app')
  assert.equal(installedAppDisplayName(null), 'Installed app')
})

test('skillContentPath: flat skills read <id>.md, directory skills read <id>/SKILL.md', () => {
  assert.equal(skillContentPath({ id: 'cron', is_dir: false }), '/api/storage/shared/skills/cron.md')
  assert.equal(skillContentPath({ id: 'skill-creator', is_dir: true }), '/api/storage/shared/skills/skill-creator/SKILL.md')
})

test('skillContentPath: the id is URL-encoded, never path-spliced raw', () => {
  assert.equal(skillContentPath({ id: 'a b', is_dir: false }), '/api/storage/shared/skills/a%20b.md')
  assert.equal(skillContentPath({ id: 'x/y', is_dir: true }), '/api/storage/shared/skills/x%2Fy/SKILL.md')
})

test('provenanceChip: each provenance family maps to a stable kind + readable label', () => {
  assert.deepEqual(provenanceChip('seed').kind, 'seed')
  assert.equal(provenanceChip('seed').label, 'built-in')
  assert.equal(provenanceChip('agent').kind, 'agent')
  assert.deepEqual(provenanceChip('app:memory'), { kind: 'app', label: 'app · memory', title: 'Owned by the memory app' })
  const inst = provenanceChip('installed:anthropics/skills')
  assert.equal(inst.kind, 'installed')
  assert.equal(inst.label, 'anthropics/skills')
})

test('provenanceChip: unknown or missing provenance degrades to a neutral chip', () => {
  assert.equal(provenanceChip('').kind, 'other')
  assert.equal(provenanceChip(undefined).kind, 'other')
  assert.equal(provenanceChip('installed:').label, 'installed')
})

test('isUninstallable: only installed:* provenance may be removed in-app', () => {
  assert.equal(isUninstallable('installed:anthropics/skills'), true)
  assert.equal(isUninstallable('seed'), false)
  assert.equal(isUninstallable('agent'), false)
  assert.equal(isUninstallable('app:memory'), false)
  assert.equal(isUninstallable(undefined), false)
})

test('skillDisplayTitle: slugs become Title Case; real names pass through', () => {
  assert.equal(skillDisplayTitle('finding-skills'), 'Finding Skills')
  assert.equal(skillDisplayTitle('skill_creator'), 'Skill Creator')
  assert.equal(skillDisplayTitle('PDF Processing'), 'PDF Processing')
  assert.equal(skillDisplayTitle(''), 'Untitled skill')
  assert.equal(skillDisplayTitle(null), 'Untitled skill')
})

test('usageLabel: zero/invalid usage renders nothing, positive counts read naturally', () => {
  assert.equal(usageLabel(0), '')
  assert.equal(usageLabel(undefined), '')
  assert.equal(usageLabel(-2), '')
  assert.equal(usageLabel(7), '7× in 30d')
})
