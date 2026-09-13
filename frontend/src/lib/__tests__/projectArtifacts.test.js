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
  assert.equal(artifactTypeName({ builder: 'latex' }), 'Artifact')
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
