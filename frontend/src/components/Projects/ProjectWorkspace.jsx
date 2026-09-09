import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Chat as MessageSquare, ChatCompose as MessageSquarePlus, Members as Users } from '@openai/apps-sdk-ui/components/Icon'
import { api, jsonOrThrow } from '../../api/client.js'
import { useHistoryDismiss } from '../../hooks/useHistoryDismiss.jsx'
import { projectQueries } from '../../hooks/queries.js'
import { queueArtifactBuildsAfterSourceChange } from '../../lib/projectArtifacts.js'
import ProjectThemeResource, { ProjectThemeSourceView } from './ProjectThemeResource.jsx'
import { linkedProjectAppId } from '../../lib/appSourceProject.js'
import ProjectArtifacts from './ProjectArtifacts.jsx'
import ProjectFinder from './ProjectFinder.jsx'
import ProjectIdentityIcon from './ProjectIdentityIcon.jsx'
import ProjectCollaborationPanel from './ProjectCollaborationPanel.jsx'
import ProjectActivityPanel from './ProjectActivityPanel.jsx'
import ProjectGitPanel from './ProjectGitPanel.jsx'
import './Projects.css'

// A project is one ordered workspace: artifacts and conversations give context
// to the source tree below them, while the selected file owns the preview pane.
// An artifact itself is not viewed here: it opens in its own independent tab.
export default function ProjectWorkspace({
  project,
  linkedApp,
  onOpenApp,
  onOpenChat,
  onCreateChat,
  onOpenArtifact,
  startRenaming = false,
  onRename,
  onRenameEnd,
}) {
  const isAppProject = !!(linkedApp || linkedProjectAppId(project))
  const [error, setError] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState(project.name)
  const [renameBusy, setRenameBusy] = useState(false)
  const [creatingChat, setCreatingChat] = useState(false)
  const [collaborationOpen, setCollaborationOpen] = useState(false)
  const [activityOpen, setActivityOpen] = useState(false)
  const [themeOpen, setThemeOpen] = useState(false)
  const themeHistory = useHistoryDismiss(() => setThemeOpen(false))
  const collaborationHistory = useHistoryDismiss(() => setCollaborationOpen(false))
  const activityHistory = useHistoryDismiss(() => setActivityOpen(false))
  const [gitOpen, setGitOpen] = useState(false)
  const [requestedFile, setRequestedFile] = useState(null)
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
    setRequestedFile(null)
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

  async function createChat(options) {
    if (creatingChat) return
    setCreatingChat(true)
    setError('')
    try {
      await onCreateChat?.(options)
    } catch (cause) {
      setError(cause?.message || 'Could not create a project chat.')
    } finally {
      setCreatingChat(false)
    }
  }

  // Turn a file the owner picked in the finder into a build artifact. The finder
  // already decided the builder from the extension; the id is derived from the
  // path so re-running "build as …" on the same file reuses one artifact rather
  // than piling up duplicates. Then build it and open it in its own viewer.
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
      'Artifact refresh failed:',
    )
    const outcomes = await queueArtifactBuildsAfterSourceChange(
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
          <button type="button" className="project-workspace__collaborate" aria-label="Share project" aria-expanded={collaborationOpen} onClick={() => { collaborationHistory.open(); setCollaborationOpen(true) }}><Users width={17} height={17} aria-hidden="true" /><span>Share</span></button>
          <button type="button" className="project-workspace__collaborate" aria-label="Project activity" aria-expanded={activityOpen} onClick={() => { activityHistory.open(); setActivityOpen(true) }}><span>Activity{activeWorkCount ? ` · ${activeWorkCount}` : ''}</span></button>
        </div>

      </div>

      {isAppProject && !linkedApp && <p className="project-source-notice">The linked app is unavailable. Source files are preserved; updating is unavailable.</p>}
      {error && <p className="projects-error" role="alert">{error}</p>}

      <div className="project-workspace__view">
        <div className="project-workspace__files" inert={themeOpen || undefined} aria-hidden={themeOpen || undefined}>
        <ProjectFinder
          projectId={project.id}
          projectName={project.name}
          artifactTypes={project.template?.artifact_types}
          onBuildFile={buildFileAsArtifact}
          excludedBuilders={isAppProject ? ['app'] : []}
          sourceDescription={isAppProject ? 'Open app to use the running version. Save edits here, then Build & update app when ready.' : undefined}
          onSourceChanged={isAppProject ? undefined : rebuildRegisteredArtifacts}
          requestedFile={requestedFile}
          onOpenGit={() => setGitOpen(true)}
          resources={<ProjectThemeResource onOpen={() => { themeHistory.open(); setThemeOpen(true) }} />}
          overview={(
            <div className="project-overview" aria-label="Project overview">
              <section className="project-overview__section" aria-labelledby={`project-artifacts-heading-${project.id}`}>
                <header className="project-overview__heading">
                  <h2 id={`project-artifacts-heading-${project.id}`}>Artifacts</h2>
                </header>
                <ProjectArtifacts
                  projectId={project.id}
                  linkedApp={linkedApp}
                  onOpenApp={onOpenApp}
                  onOpen={artifactId => onOpenArtifact?.(artifactId)}
                  onEditSource={path => setRequestedFile({ path, key: Date.now() })}
                />
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
                    <MessageSquarePlus width={16} height={16} aria-hidden="true" /><span>New chat</span>
                  </button>
                </header>
                {project.template?.actions?.length > 0 && <div className="project-overview__prompts" aria-label="Project app suggestions">{project.template.actions.map(action => (
                  <button type="button" key={action.id} disabled={creatingChat} onClick={() => void createChat({ title: action.name, prompt: action.prompt })}>{action.name}</button>
                ))}<small>Opens a draft for you to review and send.</small></div>}
                {chatsQuery.isLoading ? (
                  <p className="projects-empty" role="status">Loading chats…</p>
                ) : chatsQuery.isError ? (
                  <div className="projects-empty" role="alert"><p>Chats are unavailable.</p><button type="button" onClick={() => chatsQuery.refetch()}>Try again</button></div>
                ) : chats.length === 0 ? (
                  <p className="projects-empty">Describe what you want to make in a new chat. It can work with all the files in this project.</p>
                ) : (
                  <div className="project-chats__list">
                    {chats.map(chat => (
                      <button key={chat.id} type="button" className="project-chats__row" onClick={() => onOpenChat?.(chat)}>
                        <MessageSquare width={16} height={16} aria-hidden="true" />
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
        {themeOpen && <div className="project-workspace__resource-view"><ProjectThemeSourceView projectId={project.id} linkedApp={isAppProject} onClose={themeHistory.close} /></div>}
      </div>
      {collaborationOpen && <ProjectCollaborationPanel project={project} onClose={collaborationHistory.close} onOpenGithub={() => { collaborationHistory.close(); setGitOpen(true) }} />}
      {activityOpen && <ProjectActivityPanel project={project} onClose={activityHistory.close} />}
      {gitOpen && <ProjectGitPanel project={project} onOpenChat={onOpenChat} onClose={() => setGitOpen(false)} />}
    </section>
  )
}
