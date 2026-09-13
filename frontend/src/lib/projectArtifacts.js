// Pure helpers for project artifacts (buildable, app-contributed outputs).
//
// Kept dependency-free (no React, no DOM, no api client) so the artifact-tab
// state machine is unit-testable and the same rules drive the Artifacts list,
// the ArtifactWorkspace preview, and the build-status live update.

// Lenient read: the backend may return a bare array OR an envelope, and an
// agent may hand-edit `artifacts_json` into something malformed. Never throw —
// keep only the well-formed rows so a single bad entry can't blank the list.
export function normalizeArtifacts(data) {
  const rows = Array.isArray(data)
    ? data
    : (Array.isArray(data?.artifacts) ? data.artifacts : [])
  return rows.filter(row => (
    row && typeof row === 'object'
    && typeof row.id === 'string' && row.id.length > 0
  ))
}

export function queueArtifactBuildsAfterSourceChange(data, queueBuild) {
  const ready = normalizeArtifacts(data).filter(
    artifact => artifact.status !== 'building' && !artifact.source_missing,
  )
  return Promise.allSettled(ready.map(artifact => queueBuild(artifact.id)))
}

// The four build states map to a small status vocabulary the pill renders.
// An unknown/absent status reads as idle rather than erroring.
export function artifactStatus(artifact) {
  const status = artifact?.status
  return ['idle', 'building', 'ok', 'error'].includes(status) ? status : 'idle'
}

export function isBuilding(artifact) {
  return artifactStatus(artifact) === 'building'
}

const BUILTIN_ARTIFACT_TYPES = [
  { id: 'app', name: 'App', extensions: ['jsx', 'tsx'], preview: 'html' },
]

// Template declarations come from installed apps. Read them leniently because
// agent-authored snapshots can be malformed. The platform contributes only its
// generic App type; every domain builder belongs to its provider declaration.
export function normalizeArtifactTypes(value) {
  if (!Array.isArray(value)) return []
  return value.filter(type => (
    type && typeof type === 'object'
    && typeof type.id === 'string' && type.id.length > 0
    && typeof type.name === 'string' && type.name.length > 0
    && Array.isArray(type.extensions) && type.extensions.length > 0
    && ['html', 'pdf', 'image'].includes(type.preview)
  )).map(type => ({
    id: type.id,
    name: type.name,
    extensions: type.extensions
      .filter(ext => typeof ext === 'string')
      .map(ext => ext.toLowerCase()),
    preview: type.preview,
  }))
}

export function artifactTypeForFile(path, declaredTypes, excludedBuilders = []) {
  const extension = String(path ?? '').split('.').pop()?.toLowerCase() || ''
  const types = [
    ...normalizeArtifactTypes(declaredTypes),
    ...BUILTIN_ARTIFACT_TYPES,
  ]
  return types.find(type => !excludedBuilders.includes(type.id) && type.extensions.includes(extension)) || null
}

export function artifactTypeName(artifact) {
  if (typeof artifact?.type_name === 'string' && artifact.type_name) {
    return artifact.type_name
  }
  return artifact?.builder === 'app' ? 'App' : 'Artifact'
}

export function artifactPreviewKind(artifact) {
  if (['html', 'pdf', 'image'].includes(artifact?.preview)) return artifact.preview
  return artifact?.builder === 'app' ? 'html' : null
}

// Visual identity follows the declared output transport. Domain-specific
// semantics belong to the provider, not a shell-side word/extension classifier.
export function artifactVisualKind(artifact) {
  if (artifact?.builder === 'app') return 'mini-app'
  const preview = artifactPreviewKind(artifact)
  return ['html', 'pdf', 'image'].includes(preview) ? preview : 'artifact'
}

// Human label + a semantic variant for the status pill. Variants are stable
// class suffixes (`.artifact-pill--<variant>`), not colors, so the stylesheet
// owns the palette.
export function artifactStatusPill(artifact) {
  switch (artifactStatus(artifact)) {
    case 'building': return { label: 'Building…', variant: 'building' }
    case 'ok': return { label: 'Built', variant: 'ok' }
    case 'error': return { label: 'Build failed', variant: 'error' }
    default: return { label: 'Not built', variant: 'idle' }
  }
}

// The path WITHIN `artifacts/<id>/output/` that the preview should load. The
// backend resolves each provider's output declaration into `output_rel`; there
// is no second shell-owned interpretation of a missing declaration.
export function artifactEntryPath(artifact) {
  const outputRel = String(artifact?.output_rel ?? '')
  const marker = '/output/'
  const at = outputRel.indexOf(marker)
  const withinOutput = at !== -1 ? outputRel.slice(at + marker.length) : ''
  if (withinOutput && !withinOutput.endsWith('/')) return withinOutput
  return null
}

// Identity of the output currently safe to display. Build-status events can
// coalesce when a fast build moves ok -> building -> ok between two query
// reads. The durable completion timestamp changes for every successful build,
// so keying the preview to it reloads the actual output even when the transient
// `building` state was never observed.
export function artifactPreviewRevision(artifact) {
  if (artifactStatus(artifact) !== 'ok' || !artifact?.has_output) return ''
  return String(artifact.updated_at || 'built-output')
}

// A build-status system event addressed at THIS project. The backend event
// shape is coordinated via the build spec's event section; this reads it
// leniently (any of the plausible id field names) so a small naming difference
// on the backend does not silently drop live updates.
export function isArtifactBuildEvent(ev) {
  if (!ev || typeof ev !== 'object') return false
  return ev.type === 'artifact_build_status'
    || ev.type === 'project_artifact_build'
    || ev.type === 'artifact_build'
}

export function buildEventProjectId(ev) {
  const raw = ev?.projectId ?? ev?.project_id ?? null
  return raw == null ? null : String(raw)
}

export function buildEventArtifactId(ev) {
  const raw = ev?.artifactId ?? ev?.artifact_id ?? null
  return raw == null ? null : String(raw)
}
