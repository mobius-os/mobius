import { useEffect, useMemo, useRef, useState } from 'react'
import { Search } from '@openai/apps-sdk-ui/components/Icon'
import ProjectCreateMenu from './ProjectCreateMenu.jsx'
import ProjectActions from './ProjectActions.jsx'
import ProjectIdentityIcon from './ProjectIdentityIcon.jsx'
import ProjectTypeIcon from './ProjectTypeIcon.jsx'
import './Projects.css'

// The Projects launcher: one readable list plus the focused creation menu.
export default function ProjectsDirectory({
  projects,
  templates,
  status,
  onRetry,
  onOpen,
  onCreate,
  onImportGithub,
  onImportSource,
  onRename,
  onColor,
  onDelete,
}) {
  const [query, setQuery] = useState('')
  const visible = useMemo(() => projects.filter(project =>
    [project.name, project.template?.name, project.template?.source_app_name].filter(Boolean)
      .join(' ').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())), [projects, query])
  return (
    <section className="projects-directory" aria-label="Projects">
      <header className="projects-directory__header">
        <div>
          <h1>Projects</h1><p>Your files, chats and Creations.</p>
        </div>
        <div className="projects-directory__actions">
          <ProjectCreateMenu showLabel templates={templates} onCreate={onCreate} onImportGithub={onImportGithub} onImportSource={onImportSource} className="projects-add-menu" />
        </div>
      </header>
      {projects.length > 0 && <label className="projects-directory__search"><Search width={18} height={18} aria-hidden="true" /><input type="search" aria-label="Find a project" placeholder="Find a project" value={query} onChange={event => setQuery(event.target.value)} /></label>}
      <div className="projects-directory__scroll">
        {status === 'loading' ? (
          <p className="projects-empty" role="status">Loading projects…</p>
        ) : status === 'error' ? (
          <div className="projects-empty" role="alert">
            <p>Projects are unavailable.</p>
            <button type="button" onClick={onRetry}>Try again</button>
          </div>
        ) : projects.length === 0 ? (
          <div className="projects-empty">
            <ProjectTypeIcon value="blank" size={42} strokeWidth={1.4} aria-hidden="true" />
            <h2>A place to make something.</h2><p>Start with a template, then use project chats to shape it. Files and Creations stay together.</p>
            <button type="button" onClick={() => onCreate?.(templates[0] || { key: 'blank', name: 'Blank project' })}>Create a project</button>
          </div>
        ) : visible.length === 0 ? (
          <div className="projects-empty"><p>No projects match “{query}”.</p><button type="button" onClick={() => setQuery('')}>Clear search</button></div>
        ) : (
          <div className="projects-collection projects-collection--list">
            {visible.map(project => (
              <ProjectDirectoryRow
                key={project.id}
                project={project}
                onOpen={onOpen}
                onRename={onRename}
                onColor={onColor}
                onDelete={onDelete}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

function ProjectDirectoryRow({ project, onOpen, onRename, onColor, onDelete }) {
  const [renaming, setRenaming] = useState(false)
  const [value, setValue] = useState(project.name)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const rowRef = useRef(null)
  const inputRef = useRef(null)

  useEffect(() => {
    if (!renaming) setValue(project.name)
  }, [project.name, renaming])

  useEffect(() => {
    if (!renaming) return undefined
    const frame = requestAnimationFrame(() => inputRef.current?.select())
    return () => cancelAnimationFrame(frame)
  }, [renaming])

  async function save(event) {
    event.preventDefault()
    const next = value.trim()
    if (!next || next === project.name) {
      setValue(project.name)
      setRenaming(false)
      return
    }
    setBusy(true)
    setError('')
    try {
      await onRename?.(project, next)
      setRenaming(false)
    } catch (cause) {
      setError(cause?.message || 'Could not rename this project.')
      requestAnimationFrame(() => inputRef.current?.focus())
    } finally {
      setBusy(false)
    }
  }

  return (
    <div ref={rowRef} className={`projects-collection__row${renaming ? ' projects-collection__row--editing' : ''}`}>
      {renaming ? (
        <form className="projects-collection__rename" onSubmit={save}>
          <ProjectIdentityIcon project={project} size={36} />
          <label>
            <span className="sr-only">Project name</span>
            <input ref={inputRef} value={value} maxLength={256} disabled={busy} onChange={event => setValue(event.target.value)} onKeyDown={event => {
              if (event.key !== 'Escape' || busy) return
              setValue(project.name)
              setRenaming(false)
            }} />
            {error && <small role="alert">{error}</small>}
          </label>
          <button type="submit" disabled={busy || !value.trim()}>Save</button>
          <button type="button" disabled={busy} onClick={() => { setValue(project.name); setRenaming(false) }}>Cancel</button>
        </form>
      ) : (
        <button type="button" className="projects-collection__main" onClick={() => onOpen(project)}>
          <ProjectIdentityIcon project={project} size={36} />
          <span className="projects-collection__copy">
            <strong>{project.name}</strong>
            <small>{project.template?.name || 'Project'}{project.updated_at && ` · ${new Date(project.updated_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`}</small>
          </span>
        </button>
      )}
      {!renaming && (
        <ProjectActions
          project={project}
          contextTargetRef={rowRef}
          onRename={() => setRenaming(true)}
          onColor={onColor}
          onDelete={onDelete}
        />
      )}
    </div>
  )
}
