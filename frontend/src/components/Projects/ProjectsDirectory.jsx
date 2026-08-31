import { useEffect, useRef, useState } from 'react'
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
  onRename,
  onColor,
  onDelete,
}) {
  return (
    <section className="projects-directory" aria-label="Projects">
      <header className="projects-directory__header">
        <div>
          <h1>Projects</h1>
        </div>
        <div className="projects-directory__actions">
          <ProjectCreateMenu templates={templates} onCreate={onCreate} onImportGithub={onImportGithub} className="projects-add-menu" />
        </div>
      </header>
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
            <p>No projects yet.</p>
            <button type="button" onClick={() => onCreate?.(templates[0] || { key: 'blank', name: 'Blank project' })}>Create a project</button>
          </div>
        ) : (
          <div className="projects-collection projects-collection--list">
            {projects.map(project => (
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
            <small>{project.template?.name || project.project_type}</small>
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
