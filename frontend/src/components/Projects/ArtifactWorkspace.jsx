import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, SettingsWrench as Hammer } from '@openai/apps-sdk-ui/components/Icon'
import { api, jsonOrThrow } from '../../api/client.js'
import { projectQueries } from '../../hooks/queries.js'
import {
  artifactEntryPath,
  artifactPreviewRevision,
  artifactPreviewKind,
  artifactStatus,
  artifactStatusPill,
  artifactTypeName,
  isBuilding,
  normalizeArtifacts,
} from '../../lib/projectArtifacts.js'
import ProjectPdfPreview from './ProjectPdfPreview.jsx'
import { linkedProjectAppId } from '../../lib/appSourceProject.js'
import { assembleProjectHtmlPreview } from '../../lib/projectPreview.js'
import ArtifactIdentityIcon from './ArtifactIdentityIcon.jsx'
import ProjectPreviewFrame from './ProjectPreviewFrame.jsx'
import './Projects.css'

// The independent artifact viewer: the built result and nothing else. HTML
// renders in a sandboxed iframe with NO allow-same-origin (its JS can never
// read the parent token); PDFs render through pdfjs; images use a private
// object URL. The preview hot-swaps when a build finishes. In the shell the
// pane tab is the only chrome; the collaborator route, which has no tabs,
// passes `onOpenProject` and gets one compact back bar.
export default function ArtifactWorkspace({ projectId, project, onOpenApp, artifactId, projectName, onOpenProject, readOnly = false }) {
  const artifactsQuery = useQuery({
    queryKey: projectQueries.keys.artifacts(projectId),
    queryFn: async ({ signal }) => normalizeArtifacts(await jsonOrThrow(
      await api.projects.artifacts(projectId, { signal }),
      'Project artifacts failed:',
    )),
    // The owner shell also forwards build-status events, but an invited
    // collaborator runs in an isolated route without that stream. Poll only
    // while this exact artifact is building so both surfaces hot-swap reliably.
    refetchInterval: query => {
      const current = (query.state.data || []).find(
        row => String(row.id) === String(artifactId),
      )
      return current?.status === 'building' ? 900 : false
    },
  })
  const artifact = useMemo(
    () => (artifactsQuery.data || []).find(row => String(row.id) === String(artifactId)) || null,
    [artifactsQuery.data, artifactId],
  )
  const status = artifactStatus(artifact)
  const pill = artifactStatusPill(artifact)
  const preview = artifactPreviewKind(artifact)
  const hasOutput = !!artifact?.has_output

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const successfulPreviewRevision = artifactPreviewRevision(artifact)
  const previewVersionRef = useRef('')
  // Keep the last successful output mounted while its replacement builds.
  // The next durable completion revision replaces it even if the transient
  // building state was coalesced out of the query stream.
  if (successfulPreviewRevision) {
    previewVersionRef.current = successfulPreviewRevision
  }
  const previewVersion = previewVersionRef.current

  async function build() {
    if (busy || isBuilding(artifact)) return
    setBusy(true); setError('')
    try {
      await jsonOrThrow(await api.projects.buildArtifact(projectId, artifactId), 'Build failed:')
      await artifactsQuery.refetch()
    } catch (cause) {
      setError(cause?.message || 'Could not start the build.')
    } finally { setBusy(false) }
  }

  const entryPath = artifact ? artifactEntryPath(artifact) : null
  const name = artifact?.name || artifactId

  if (!artifactsQuery.isLoading && !artifact && linkedProjectAppId(project) && project.template?.retired_app_previews?.includes(artifactId)) {
    return <section className="artifact-workspace"><div className="projects-empty">
      <p>This Project now opens the installed app, not a separate preview.</p>
      {onOpenApp && <button type="button" onClick={() => onOpenApp(linkedProjectAppId(project))}>Open app</button>}
      {onOpenProject && <button type="button" onClick={onOpenProject}>Back to project</button>}
    </div></section>
  }
  if (artifactsQuery.isLoading) {
    return <section className="artifact-workspace" aria-busy="true"><p className="projects-empty" role="status">Loading Creation…</p></section>
  }
  if (!artifact) {
    return (
      <section className="artifact-workspace">
        <div className="projects-empty" role="alert">
          <p>This Creation is no longer available.</p>
          {onOpenProject && <button type="button" onClick={onOpenProject}>Back to project</button>}
        </div>
      </section>
    )
  }

  return (
    <section className="artifact-workspace" aria-label={`${name} Creation`}>
      {onOpenProject && <header className="artifact-workspace__header">
        <button type="button" className="project-icon-button" aria-label="Back to project" title={projectName ? `Back to ${projectName}` : 'Back to project'} onClick={onOpenProject}><ArrowLeft width={18} height={18} /></button>
        <ArtifactIdentityIcon artifact={artifact} size={34} />
        <div className="artifact-workspace__identity">
          <strong>{name}</strong>
          <small>{artifactTypeName(artifact)}</small>
        </div>
        <span className={`artifact-pill artifact-pill--${pill.variant}`} role="status">{pill.label}</span>
      </header>}

      {error && <p className="projects-error" role="alert">{error}</p>}

      {hasOutput && status === 'error' && <p className="projects-error" role="status">The latest build failed. Showing the last successful Creation.</p>}
      <div className="artifact-workspace__surface">
        {!hasOutput ? (
          <div className="project-document__empty" role="status">
            <Hammer width={42} height={42} aria-hidden="true" />
            <h2>{status === 'building' ? 'Building your Creation…' : 'Nothing built yet'}</h2>
            <p>{status === 'building' ? 'The preview will appear here when it is ready.' : status === 'error' ? 'The last build failed. Fix the source and build again.' : 'Build this Creation to see it here.'}</p>
            {!readOnly && <button type="button" className="project-build-button" disabled={busy || status === 'building'} onClick={build}><Hammer width={16} height={16} aria-hidden="true" /><span>Build</span></button>}
          </div>
        ) : preview === 'pdf' ? (
          <PdfPreview projectId={projectId} artifactId={artifactId} entryPath={entryPath} version={previewVersion} />
        ) : preview === 'image' ? (
          <ImagePreview projectId={projectId} artifactId={artifactId} entryPath={entryPath} version={previewVersion} name={name} />
        ) : (
          <WebsitePreview projectId={projectId} artifactId={artifactId} sourcePath={artifact.source || entryPath} entryPath={entryPath} version={previewVersion} name={name} />
        )}
      </div>
    </section>
  )
}

// The static-site preview. The shell fetches the built output over the
// authenticated route (Bearer) and inlines the entry's local CSS/JS into a
// self-contained srcDoc on an opaque origin — never a token-bearing src URL a
// sandboxed artifact could read from window.location. The frame is sandboxed
// allow-scripts WITHOUT allow-same-origin (real sites run; their JS cannot reach
// the owner token). Uninlined/remote refs are CSP-blocked so a missing local
// dependency stays visible rather than silently loading. A superseded fetch is
// aborted; `version` re-assembles after a rebuild (the hot-swap).
function WebsitePreview({ projectId, artifactId, sourcePath, entryPath, version, name }) {
  const [doc, setDoc] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const entry = entryPath || 'index.html'
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setLoading(true); setError('')
    ;(async () => {
      try {
        const res = await api.projects.artifactOutput(projectId, artifactId, entry, { signal: controller.signal })
        if (!active) return
        if (!res.ok) throw new Error(`The Creation could not be loaded (${res.status}).`)
        const html = await res.text()
        const loadText = async (path) => {
          const dep = await api.projects.artifactOutput(projectId, artifactId, path, { signal: controller.signal })
          if (!dep.ok) throw new Error(`dependency ${path} failed (${dep.status})`)
          return dep.text()
        }
        // Local images/fonts/backgrounds become data: URIs so the site renders
        // fully inside the opaque sandboxed srcDoc (relative URLs are CSP-blocked).
        const loadDataUri = async (path) => {
          const dep = await api.projects.artifactOutput(projectId, artifactId, path, { signal: controller.signal })
          if (!dep.ok) throw new Error(`asset ${path} failed (${dep.status})`)
          const blob = await dep.blob()
          return await new Promise((resolve, reject) => {
            const reader = new FileReader()
            reader.onload = () => resolve(reader.result)
            reader.onerror = () => reject(reader.error)
            reader.readAsDataURL(blob)
          })
        }
        const assembled = await assembleProjectHtmlPreview(html, entry, loadText, loadDataUri)
        if (active) setDoc(assembled)
      } catch (cause) {
        if (active && cause?.name !== 'AbortError') setError(cause?.message || 'The Creation could not be loaded.')
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => { active = false; controller.abort() }
  }, [projectId, artifactId, entry, version])

  if (error) return <div className="project-document__empty" role="alert"><h2>Couldn’t load this Creation</h2><p>{error}</p></div>
  if (loading && !doc) return <div className="project-document__empty" role="status"><p>Loading Creation…</p></div>
  return (
    <div className="artifact-preview">
      <ProjectPreviewFrame
        key={`${artifactId}:${version}`}
        projectId={projectId}
        sourcePath={sourcePath || entry}
        title={`${name} preview`}
        className="artifact-preview__frame"
        srcDoc={doc}
      />
    </div>
  )
}

// The PDF preview: fetch compiled bytes through the authenticated output route
// (pdfjs cannot send the Bearer itself) and render via pdfjs.
// A superseded fetch is aborted; `version` re-fetches after a rebuild.
function PdfPreview({ projectId, artifactId, entryPath, version }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setLoading(true); setError('')
    ;(async () => {
      try {
        const res = await api.projects.artifactOutput(projectId, artifactId, entryPath || 'main.pdf', { signal: controller.signal })
        if (!active) return
        if (!res.ok) throw new Error(`The document could not be loaded (${res.status}).`)
        const bytes = new Uint8Array(await res.arrayBuffer())
        if (active) setData(bytes)
      } catch (cause) {
        if (active && cause?.name !== 'AbortError') setError(cause?.message || 'The document could not be loaded.')
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => { active = false; controller.abort() }
  }, [projectId, artifactId, entryPath, version])

  if (error) return <div className="project-document__empty" role="alert"><h2>Couldn’t load the document</h2><p>{error}</p></div>
  if (loading && !data) return <div className="project-document__empty" role="status"><p>Rendering document…</p></div>
  return <ProjectPdfPreview data={data} title="Creation document" />
}

function ImagePreview({ projectId, artifactId, entryPath, version, name }) {
  const [src, setSrc] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const srcRef = useRef('')
  useEffect(() => () => {
    if (srcRef.current) URL.revokeObjectURL(srcRef.current)
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setLoading(true); setError('')
    ;(async () => {
      try {
        const res = await api.projects.artifactOutput(
          projectId, artifactId, entryPath || 'preview.png',
          { signal: controller.signal },
        )
        if (!res.ok) throw new Error(`The image could not be loaded (${res.status}).`)
        const objectUrl = URL.createObjectURL(await res.blob())
        if (active) {
          const previous = srcRef.current
          srcRef.current = objectUrl
          setSrc(objectUrl)
          if (previous) URL.revokeObjectURL(previous)
        } else {
          URL.revokeObjectURL(objectUrl)
        }
      } catch (cause) {
        if (active && cause?.name !== 'AbortError') {
          setError(cause?.message || 'The image could not be loaded.')
        }
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
      controller.abort()
    }
  }, [projectId, artifactId, entryPath, version])

  if (error) return <div className="project-document__empty" role="alert"><h2>Couldn’t load the image</h2><p>{error}</p></div>
  if (loading && !src) return <div className="project-document__empty" role="status"><p>Loading image…</p></div>
  return <div className="artifact-preview artifact-preview--image"><img src={src} alt={`${name} preview`} /></div>
}
