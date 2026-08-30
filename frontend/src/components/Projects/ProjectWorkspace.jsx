import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import MessageSquare from 'lucide-react/dist/esm/icons/message-square.mjs'
import MessageSquarePlus from 'lucide-react/dist/esm/icons/message-square-plus.mjs'
import Github from 'lucide-react/dist/esm/icons/folder-git-2.mjs'
import Users from 'lucide-react/dist/esm/icons/users.mjs'
import { api, jsonOrThrow } from '../../api/client.js'
import { projectQueries } from '../../hooks/queries.js'
import { queueArtifactBuildsAfterSourceSave } from '../../lib/projectArtifacts.js'
import ProjectArtifacts from './ProjectArtifacts.jsx'
import ProjectFinder from './ProjectFinder.jsx'
import ProjectIdentityIcon from './ProjectIdentityIcon.jsx'
import ProjectCollaborationPanel from './ProjectCollaborationPanel.jsx'
import ProjectGitPanel from './ProjectGitPanel.jsx'
import './Projects.css'

// A project is one ordered workspace: outputs and conversations give context
// to the source tree below them, while the selected file owns the preview pane.
export default function ProjectWorkspace({
  project,
  onOpenChat,
  onCreateChat,
  onOpenArtifact,
  startRenaming = false,
  onRename,
  onRenameEnd,
}) {
  const [error, setError] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState(project.name)
  const [renameBusy, setRenameBusy] = useState(false)
  const [creatingChat, setCreatingChat] = useState(false)
  const [collaborationOpen, setCollaborationOpen] = useState(false)
  const [gitOpen, setGitOpen] = useState(false)
  const renameInputRef = useRef(null)
  const queryClient = useQueryClient()

  const chatsQuery = useQuery({
    queryKey: projectQueries.keys.chats(project.id),
    queryFn: async () => {
      const rows = await jsonOrThrow(await api.projects.chats(project.id), 'Project chats failed:')
      return Array.isArray(rows) ? rows : []
    },
    initialData: Array.isArray(project.chats) ? project.chats : undefined,
  })
  const chats = chatsQuery.data || []
  const claimsQuery = useQuery({
    queryKey: projectQueries.keys.workClaims(project.id),
    queryFn: async () => jsonOrThrow(
      await api.projects.workClaims(project.id), 'Active work failed:',
    ),
    refetchInterval: 10_000,
  })
  const activeWorkCount = claimsQuery.data?.claims?.length || 0

  useEffect(() => {
    setError('')
    setRenaming(false)
  }, [project.id])

  useEffect(() => {
    if (!renaming) setRenameValue(project.name)
  }, [project.name, renaming])

  useEffect(() => {
    if (!startRenaming) return
    setRenameValue(project.name)
    setRenaming(true)
    const frame = requestAnimationFrame(() => renameInputRef.current?.select())
    return () => cancelAnimationFrame(frame)
  }, [project.id, project.name, startRenaming])

  async function saveProjectName() {
    if (renameBusy) return
    const next = renameValue.trim()
    if (!next || next === project.name) {
      setRenameValue(project.name)
      setRenaming(false)
      onRenameEnd?.()
      return
    }
    setRenameBusy(true)
    setError('')
    try {
      await onRename?.(next)
      setRenaming(false)
      onRenameEnd?.()
    } catch (cause) {
      setError(cause?.message || 'Could not rename this project.')
      requestAnimationFrame(() => renameInputRef.current?.focus())
    } finally {
      setRenameBusy(false)
    }
  }

  async function createChat() {
    if (creatingChat) return
    setCreatingChat(true)
    setError('')
    try {
      await onCreateChat?.()
    } catch (cause) {
      setError(cause?.message || 'Could not create a project chat.')
    } finally {
      setCreatingChat(false)
    }
  }

  // Turn a file the owner picked in the finder into a build artifact. The finder
  // already decided the builder from the extension; the id is derived from the
  // path so re-running "build as …" on the same file reuses one artifact rather
  // than piling up duplicates. Then build it and open its tab.
  async function buildFileAsArtifact(path, builder) {
    const base = (path.split('/').pop() || path).replace(/\.[^.]+$/, '') || 'output'
    const id = path.replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9_-]+/g, '-')
      .replace(/^-+|-+$/g, '').toLowerCase().slice(0, 64) || 'artifact'
    setError('')
    try {
      try {
        await jsonOrThrow(await api.projects.createArtifact(project.id, {
          id, name: base, builder, source: path,
        }), 'Build failed:')
      } catch (cause) {
        // An artifact for this file already exists — reuse it. Re-raise anything
        // that is not a duplicate (e.g. the source vanished).
        if (!/already|exist|409/i.test(cause?.message || '')) throw cause
      }
      await jsonOrThrow(await api.projects.buildArtifact(project.id, id), 'Build failed:')
      queryClient.invalidateQueries({ queryKey: projectQueries.keys.artifacts(project.id) })
      onOpenArtifact?.(id)
    } catch (cause) {
      setError(cause?.message || 'Could not build that file.')
    }
  }

  // Registered artifacts are the durable live views of this source tree. A
  // deliberate Save queues every idle artifact so edits to dependencies (CSS,
  // data.js, images) refresh the owning output just like edits to its entry.
  // Generated artifact files stay outside versioned source by backend contract.
  async function rebuildRegisteredArtifacts() {
    const artifacts = await jsonOrThrow(
      await api.projects.artifacts(project.id),
      'Creation refresh failed:',
    )
    const outcomes = await queueArtifactBuildsAfterSourceSave(
      artifacts,
      async artifactId => jsonOrThrow(
        await api.projects.buildArtifact(project.id, artifactId), 'Build failed:',
      ),
    )
    await queryClient.invalidateQueries({ queryKey: projectQueries.keys.artifacts(project.id) })
    const failed = outcomes.find(outcome => outcome.status === 'rejected')
    if (failed) throw failed.reason
  }

  return (
    <section className="project-workspace" aria-label={`${project.name} project`}>
      <div className="project-workspace__bar">
        <div className="project-workspace__identity">
          <ProjectIdentityIcon project={project} size={32} />
          {renaming ? (
            <form className="project-workspace__rename" onSubmit={event => { event.preventDefault(); renameInputRef.current?.blur() }}>
              <input
                ref={renameInputRef}
                value={renameValue}
                maxLength={256}
                aria-label="Project name"
                disabled={renameBusy}
                onChange={event => setRenameValue(event.target.value)}
                onBlur={() => void saveProjectName()}
                onKeyDown={event => {
                  if (event.key !== 'Escape' || renameBusy) return
                  event.preventDefault()
                  setRenameValue(project.name)
                  setRenaming(false)
                  onRenameEnd?.()
                }}
              />
            </form>
          ) : (
            <button
              type="button"
              className="project-workspace__title"
              title="Rename project"
              aria-label={`Rename ${project.name}`}
              onClick={() => { setRenameValue(project.name); setRenaming(true) }}
            >
              <span>{project.name}</span>
            </button>
          )}
        </div>

        <div className="project-workspace__actions">
          <button type="button" className="project-workspace__collaborate" aria-label="Review publishing" title="Review publishing" aria-expanded={gitOpen} onClick={() => setGitOpen(true)}><Github size={17} aria-hidden="true" /><span>Publish</span></button>
          <button type="button" className="project-workspace__collaborate" aria-label={activeWorkCount ? `Share and collaborate, ${activeWorkCount} active` : 'Share and collaborate'} title={activeWorkCount ? `${activeWorkCount} active in this project` : 'Share and collaborate'} aria-expanded={collaborationOpen} onClick={() => setCollaborationOpen(true)}><Users size={17} aria-hidden="true" /><span>{activeWorkCount ? `${activeWorkCount} active` : 'Collaborate'}</span></button>
        </div>

      </div>

      {error && <p className="projects-error" role="alert">{error}</p>}

      <div className="project-workspace__view">
        <ProjectFinder
          projectId={project.id}
          projectName={project.name}
          artifactTypes={project.template?.artifact_types}
          onBuildFile={buildFileAsArtifact}
          onSourceSaved={rebuildRegisteredArtifacts}
          overview={(
            <div className="project-overview" aria-label="Project overview">
              <section className="project-overview__section" aria-labelledby={`project-artifacts-heading-${project.id}`}>
                <header className="project-overview__heading">
                  <h2 id={`project-artifacts-heading-${project.id}`}>Creations</h2>
                </header>
                <ProjectArtifacts projectId={project.id} onOpen={onOpenArtifact} />
              </section>

              <section className="project-overview__section" aria-labelledby={`project-chats-heading-${project.id}`}>
                <header className="project-overview__heading">
                  <h2 id={`project-chats-heading-${project.id}`}>Chats</h2>
                  <button
                    type="button"
                    className="project-overview__action"
                    aria-label={creatingChat ? 'Creating chat…' : 'New chat'}
                    title="New chat"
                    disabled={creatingChat}
                    onClick={() => void createChat()}
                  >
                    <MessageSquarePlus size={16} aria-hidden="true" />
                  </button>
                </header>
                {chatsQuery.isLoading ? (
                  <p className="projects-empty" role="status">Loading chats…</p>
                ) : chatsQuery.isError ? (
                  <div className="projects-empty" role="alert"><p>Chats are unavailable.</p><button type="button" onClick={() => chatsQuery.refetch()}>Try again</button></div>
                ) : chats.length === 0 ? (
                  <p className="projects-empty">No chats yet.</p>
                ) : (
                  <div className="project-chats__list">
                    {chats.map(chat => (
                      <button key={chat.id} type="button" className="project-chats__row" onClick={() => onOpenChat?.(chat)}>
                        <MessageSquare size={16} aria-hidden="true" />
                        <span><strong>{chat.title || 'New chat'}</strong>{!chat.has_messages && <small>Empty</small>}</span>
                      </button>
                    ))}
                  </div>
                )}
              </section>
            </div>
          )}
        />
      </div>
      {collaborationOpen && <ProjectCollaborationPanel project={project} onClose={() => setCollaborationOpen(false)} />}
      {gitOpen && <ProjectGitPanel project={project} onClose={() => setGitOpen(false)} />}
    </section>
  )
}
