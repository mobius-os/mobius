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
  {
    id: 'website', name: 'Website', extensions: ['html', 'htm'], preview: 'html',
  },
  {
    id: 'latex', name: 'PDF', extensions: ['tex'], preview: 'pdf',
  },
]

// Template declarations come from installed apps. Read them leniently because
// old project snapshots predate artifact types and an app update must never
// make the Finder disappear. Built-ins remain the fallback for blank and old
// projects; declarations win so a provider can own its normal extension.
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

export function artifactTypeForFile(path, declaredTypes) {
  const extension = String(path ?? '').split('.').pop()?.toLowerCase() || ''
  const types = [
    ...normalizeArtifactTypes(declaredTypes),
    ...BUILTIN_ARTIFACT_TYPES,
  ]
  return types.find(type => type.extensions.includes(extension)) || null
}

export function artifactTypeName(artifact) {
  if (typeof artifact?.type_name === 'string' && artifact.type_name) {
    return artifact.type_name
  }
  const builtin = BUILTIN_ARTIFACT_TYPES.find(type => type.id === artifact?.builder)
  return builtin?.name || 'Artifact'
}

export function artifactPreviewKind(artifact) {
  if (['html', 'pdf', 'image'].includes(artifact?.preview)) return artifact.preview
  return artifact?.builder === 'latex' ? 'pdf' : 'html'
}

// A richer visual identity than the transport preview kind. Many durable
// formats compile to HTML, but a Markdown document, CSV sheet, React mini-app,
// and data visualization should not all look like generic browser windows.
export function artifactVisualKind(artifact) {
  const words = [
    artifact?.builder,
    artifact?.type_name,
    artifact?.name,
    artifact?.source,
  ].filter(Boolean).join(' ').toLowerCase()
  const preview = artifactPreviewKind(artifact)
  if (preview === 'pdf' || /latex|\.tex\b|\bpdf\b/.test(words)) return 'pdf'
  if (preview === 'image') return 'image'
  if (/slides?|presentation|\bdeck\b/.test(words)) return 'presentation'
  if (/spreadsheet|\bsheet\b|\.csv\b|\btable\b/.test(words)) return 'sheet'
  if (/document|markdown|\.md\b|writing/.test(words)) return 'document'
  if (/visuali[sz]ation|chart|dashboard|data story/.test(words)) return 'visualization'
  if (/mini.?app|react app|\.jsx\b|\.tsx\b/.test(words)) return 'mini-app'
  return 'html'
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

// The last path segment without its extension, e.g. `paper/main.tex` -> `main`.
export function fileStem(path) {
  const base = String(path ?? '').split('/').pop() || ''
  const dot = base.lastIndexOf('.')
  return dot > 0 ? base.slice(0, dot) : base
}

// The path WITHIN `artifacts/<id>/output/` that the preview should load. The
// backend resolves each provider's output declaration into `output_rel`; honor
// it for every artifact kind. Old rows without that field retain sensible
// website/PDF fallbacks.
export function artifactEntryPath(artifact) {
  const outputRel = String(artifact?.output_rel ?? '')
  const marker = '/output/'
  const at = outputRel.indexOf(marker)
  const withinOutput = at !== -1 ? outputRel.slice(at + marker.length) : ''
  if (withinOutput && !withinOutput.endsWith('/')) return withinOutput
  const preview = artifactPreviewKind(artifact)
  if (preview === 'pdf') {
    const stem = fileStem(artifact?.source) || 'main'
    return `${stem}.pdf`
  }
  if (preview === 'image') {
    return String(artifact?.source ?? '').split('/').pop() || 'preview.png'
  }
  return 'index.html'
}

// Whether the preview surface should hot-swap (reload the iframe / re-render the
// pdf) given the status BEFORE and AFTER a refresh. A finished build (building
// -> ok) is the swap trigger; a fresh `ok` first seen (no prior status, e.g. the
// tab opened after the build finished) also loads once. Same status, or a
// transition into building/error, never swaps.
export function shouldHotSwapPreview(prevStatus, nextStatus) {
  if (nextStatus !== 'ok') return false
  if (prevStatus === 'ok') return false
  return true
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
