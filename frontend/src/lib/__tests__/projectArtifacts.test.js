import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  artifactEntryPath,
  artifactPreviewKind,
  artifactPreviewRevision,
  artifactStatus,
  artifactStatusPill,
  artifactVisualKind,
  artifactTypeForFile,
  artifactTypeName,
  buildEventArtifactId,
  buildEventProjectId,
  isArtifactBuildEvent,
  isBuilding,
  normalizeArtifacts,
  queueArtifactBuildsAfterSourceChange,
} from '../projectArtifacts.js'

test('normalizeArtifacts reads a bare array or an envelope, and drops malformed rows', () => {
  assert.deepEqual(normalizeArtifacts([{ id: 'a' }, { id: 'b' }]).map(r => r.id), ['a', 'b'])
  assert.deepEqual(normalizeArtifacts({ artifacts: [{ id: 'a' }] }).map(r => r.id), ['a'])
  // Lenient read: a hand-edited manifest with junk entries never throws.
  assert.deepEqual(normalizeArtifacts([{ id: 'a' }, null, {}, { id: '' }, 'x']).map(r => r.id), ['a'])
  assert.deepEqual(normalizeArtifacts(undefined), [])
})

test('a source change queues one idle envelope artifact and preserves build filters', async () => {
  const queued = []
  const outcomes = await queueArtifactBuildsAfterSourceChange({
    artifacts: [
      { id: 'site', status: 'idle' },
      { id: 'busy', status: 'building' },
      { id: 'missing', status: 'idle', source_missing: true },
      { id: '' },
    ],
  }, async artifactId => {
    queued.push(artifactId)
  })

  assert.deepEqual(queued, ['site'])
  assert.equal(outcomes.length, 1)
  assert.equal(outcomes[0].status, 'fulfilled')
})

test('artifactStatus and pill map the four states, unknown reads as idle', () => {
  assert.equal(artifactStatus({ status: 'building' }), 'building')
  assert.equal(artifactStatus({ status: 'nonsense' }), 'idle')
  assert.equal(artifactStatus(null), 'idle')
  assert.equal(artifactStatusPill({ status: 'ok' }).variant, 'ok')
  assert.equal(artifactStatusPill({ status: 'error' }).variant, 'error')
  assert.equal(artifactStatusPill({ status: 'idle' }, { building: true }).label, 'Building…', 'pressing Build shows progress at once')
  assert.equal(artifactStatusPill({ status: 'ok', type_name: 'PDF' }, { justBuilt: true }).label, 'PDF ready · Open')
  assert.deepEqual(artifactStatusPill({ status: 'ok', out_of_date: true }, { justBuilt: true }), { label: 'Out of date', variant: 'stale' })
  assert.equal(isBuilding({ status: 'building' }), true)
  assert.equal(isBuilding({ status: 'ok' }), false)
})

test('artifactEntryPath reads only a concrete declared output', () => {
  assert.equal(artifactEntryPath({ builder: 'website' }), null)
  assert.equal(
    artifactEntryPath({ builder: 'website', output_rel: 'artifacts/site/output/index.html' }),
    'index.html',
  )
  assert.equal(
    artifactEntryPath({ builder: 'website', output_rel: 'artifacts/site/output/home.html' }),
    'home.html',
  )
})

test('artifactEntryPath does not infer a compiled path from a builder or source', () => {
  assert.equal(artifactEntryPath({ builder: 'latex', source: 'main.tex' }), null)
  assert.equal(artifactEntryPath({ builder: 'latex', source: 'paper/thesis.tex' }), null)
  assert.equal(
    artifactEntryPath({ builder: 'latex', source: 'main.tex', output_rel: 'artifacts/x/output/report.pdf' }),
    'report.pdf',
  )
})

test('project templates contribute artifact types and win over built-in extensions', () => {
  const types = [{
    id: 'poster', name: 'Poster', extensions: ['svg'], preview: 'image',
  }, {
    id: 'owned-web', name: 'Published site', extensions: ['html'], preview: 'html',
  }]
  assert.deepEqual(artifactTypeForFile('design.svg', types), types[0])
  assert.equal(artifactTypeForFile('index.html', types).id, 'owned-web')
  assert.equal(artifactTypeForFile('paper.tex', types), null)
  assert.equal(artifactTypeForFile('notes.md', types), null)
})

test('artifact presentation comes from the provider contract, not builder ids', () => {
  const custom = {
    builder: 'poster',
    type_name: 'Poster',
    preview: 'image',
    source: 'art/design.svg',
    output_rel: 'artifacts/design/output/render/final.png',
  }
  assert.equal(artifactTypeName(custom), 'Poster')
  assert.equal(artifactPreviewKind(custom), 'image')
  assert.equal(artifactEntryPath(custom), 'render/final.png')
  assert.equal(artifactTypeName({ builder: 'latex' }), 'Result')
})

test('artifact icons use only declared transport plus the platform App type', () => {
  assert.equal(artifactVisualKind({ builder: 'document', source: 'brief.md', preview: 'html' }), 'html')
  assert.equal(artifactVisualKind({ type_name: 'Presentation', preview: 'html' }), 'html')
  assert.equal(artifactVisualKind({ preview: 'pdf' }), 'pdf')
  assert.equal(artifactVisualKind({ preview: 'image' }), 'image')
  assert.equal(artifactVisualKind({ builder: 'app', preview: 'html' }), 'mini-app')
  assert.equal(artifactVisualKind({ builder: 'unknown' }), 'artifact')
})

test('artifactPreviewRevision tracks successful output rather than transient status', () => {
  const first = {
    status: 'ok', has_output: true, updated_at: '2026-09-04T18:12:51.680864',
  }
  const rebuilt = {
    ...first, updated_at: '2026-09-04T18:12:52.010222',
  }
  assert.equal(artifactPreviewRevision(first), first.updated_at)
  assert.equal(artifactPreviewRevision(rebuilt), rebuilt.updated_at)
  assert.notEqual(artifactPreviewRevision(first), artifactPreviewRevision(rebuilt))
  assert.equal(artifactPreviewRevision({ ...rebuilt, status: 'building' }), '')
  assert.equal(artifactPreviewRevision({ ...rebuilt, has_output: false }), '')
  assert.equal(artifactPreviewRevision({ status: 'ok', has_output: true }), 'built-output')
})

test('build-status event detection reads plausible id field names leniently', () => {
  assert.equal(isArtifactBuildEvent({ type: 'artifact_build_status' }), true)
  assert.equal(isArtifactBuildEvent({ type: 'project_artifact_build' }), true)
  assert.equal(isArtifactBuildEvent({ type: 'app_updated' }), false)
  assert.equal(buildEventProjectId({ projectId: 5 }), '5')
  assert.equal(buildEventProjectId({ project_id: '9' }), '9')
  assert.equal(buildEventProjectId({}), null)
  assert.equal(buildEventArtifactId({ artifactId: 'site' }), 'site')
  assert.equal(buildEventArtifactId({ artifact_id: 'doc' }), 'doc')
})

test('a standalone result is named for its project, then its type or its own name', async () => {
  const { resultDisplayName } = await import('../projectArtifacts.js')
  const pdf = { id: 'document', name: 'Document', type_name: 'PDF' }
  assert.equal(resultDisplayName({ name: 'Thesis', artifacts: [pdf] }, pdf), 'Thesis · PDF')
  const part = { id: 'part-1', name: 'Part 1', type_name: 'PDF' }
  assert.equal(resultDisplayName({ name: 'Thesis', artifacts: [pdf, part] }, part), 'Thesis · Part 1')
  assert.equal(resultDisplayName(null, part), 'Part 1')
})

test('a fresh project opens on its main result when built, else on that result\'s source', async () => {
  const { projectEntryTarget } = await import('../projectArtifacts.js')
  const template = { previews: [{ id: 'document' }] }
  const extra = { id: 'notes', source: 'notes.tex', has_output: true }
  const built = { id: 'document', source: 'main.tex', has_output: true }
  assert.deepEqual(projectEntryTarget({ template, artifacts: [extra, built] }), { kind: 'result', artifactId: 'document' })
  const fresh = { id: 'document', source: 'main.tex', has_output: false }
  assert.deepEqual(projectEntryTarget({ template, artifacts: [extra, fresh] }), { kind: 'file', path: 'main.tex' })
  assert.equal(projectEntryTarget({ template, artifacts: [{ ...fresh, source_missing: true }] }), null)
  assert.equal(projectEntryTarget({ template, artifacts: [] }), null)
})

test('the main result follows declared preview order, not registry order', async () => {
  const { projectEntryTarget } = await import('../projectArtifacts.js')
  const template = { previews: [{ id: 'primary' }, { id: 'secondary' }] }
  const artifacts = [
    { id: 'secondary', source: 'appendix.tex', has_output: true },
    { id: 'primary', source: 'main.tex', has_output: true },
  ]
  assert.deepEqual(projectEntryTarget({ template, artifacts }), { kind: 'result', artifactId: 'primary' })
  assert.deepEqual(projectEntryTarget({ template: { previews: [{ id: 'gone' }] }, artifacts }), { kind: 'result', artifactId: 'secondary' })
})

test('a collaborator source change refreshes a mounted, settled result to out of date', async () => {
  const { QueryClient, QueryObserver } = await import('@tanstack/react-query')
  const { refreshSourceViews } = await import('../projectArtifacts.js')
  const client = new QueryClient()
  const key = ['projects', 'artifacts', 'shared-7']
  let server = [{ id: 'site', status: 'ok', has_output: true, out_of_date: false }]
  const observer = new QueryObserver(client, { queryKey: key, queryFn: async () => server, staleTime: Infinity })
  const unsubscribe = observer.subscribe(() => {})
  try {
    await client.fetchQuery({ queryKey: key, queryFn: async () => server })
    assert.equal(observer.getCurrentResult().data[0].out_of_date, false)
    server = [{ ...server[0], out_of_date: true }]
    // The shared surface's own invalidator refreshes only its files and Git.
    const sharedSource = { invalidate: c => c.invalidateQueries({ queryKey: ['shared-project', 'shared-7', 'files'] }) }
    await refreshSourceViews(client, sharedSource, key)
    assert.equal(observer.getCurrentResult().data[0].out_of_date, true)
  } finally {
    unsubscribe()
    client.clear()
  }
})
