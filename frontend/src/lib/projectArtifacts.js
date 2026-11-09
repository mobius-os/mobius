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
  return artifact?.builder === 'app' ? 'App' : 'Result'
}

// Where a result stands alone (a tab, a Recents row) it is named for its
// project, so two "Document" results from different projects stay distinct.
// A project's only result reads by its type ("Thesis · PDF"); several results
// keep their own names ("Thesis · Part 1"), since the type no longer tells them apart.
export function resultDisplayName(project, artifact) {
  const resultCount = Array.isArray(project?.artifacts) ? project.artifacts.length : 0
  const result = resultCount > 1
    ? artifact?.name || artifact?.id || artifactTypeName(artifact)
    : artifactTypeName(artifact)
  return project?.name ? `${project.name} · ${result}` : (artifact?.name || result)
}

// What a freshly opened project shows first: its main result when one has been
// built, otherwise that result's source file. The main result is the first
// declared preview that is registered, in declaration order, else the first
// result. A project with no results opens on its overview.
export function projectEntryTarget(project) {
  const artifacts = Array.isArray(project?.artifacts) ? project.artifacts : []
  if (!artifacts.length) return null
  const byId = new Map(artifacts.map(row => [row?.id, row]))
  const main = (project?.template?.previews || [])
    .map(preview => byId.get(preview?.id))
    .find(Boolean) || artifacts[0]
  if (main?.has_output) return { kind: 'result', artifactId: main.id }
  if (typeof main?.source === 'string' && main.source && !main.source_missing) {
    return { kind: 'file', path: main.source }
  }
  return null
}

// Any source change can leave a built result out of date, whichever surface
// (owner or collaborator) saw it, so results refresh beside the source's views.
export function refreshSourceViews(client, source, artifactsKey) {
  return Promise.all([
    source.invalidate(client),
    client.invalidateQueries({ queryKey: artifactsKey, exact: true }),
  ])
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
//
// `building` covers the moment between pressing Build and the server's first
// status. `justBuilt` marks a build this view watched finish, which earns a
// direct "PDF ready · Open" until the result is opened. A result whose source
// changed after its build is out of date, whatever its last build did.
export function artifactStatusPill(artifact, { building = false, justBuilt = false } = {}) {
  const status = building ? 'building' : artifactStatus(artifact)
  switch (status) {
    case 'building': return { label: 'Building…', variant: 'building' }
    case 'ok':
      if (artifact?.out_of_date) return { label: 'Out of date', variant: 'stale' }
      if (justBuilt) return { label: `${artifactTypeName(artifact)} ready · Open`, variant: 'ready' }
      return { label: 'Built', variant: 'ok' }
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
