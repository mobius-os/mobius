import { useEffect, useMemo, useRef, useState } from 'react'
import { Plus, Folder, ArrowLeft, FileCode, FileDocument } from '@openai/apps-sdk-ui/components/Icon'
import { api, jsonOrThrow } from '../../api/client.js'
import { globalProjectTemplates } from '../../lib/projectTypes.js'
import ProjectTypeIcon from './ProjectTypeIcon.jsx'
import './ProjectCreateMenu.css'

const FALLBACK_TEMPLATES = [{
  key: 'blank',
  name: 'Blank project',
  description: 'Start with an empty folder.',
}]

export default function ProjectCreateMenu({
  templates,
  onCreate,
  onImportGithub,
  onImportSource,
  className = '',
  align = 'end',
  label = 'Create project',
  showLabel = false,
}) {
  const [open, setOpen] = useState(false)
  const [busyKey, setBusyKey] = useState(null)
  const [error, setError] = useState('')
  const [view, setView] = useState('types')
  const [repository, setRepository] = useState('')
  const [projectName, setProjectName] = useState('')
  const [sources, setSources] = useState(null)
  const [sourcesLoading, setSourcesLoading] = useState(false)
  const rootRef = useRef(null)
  const firstItemRef = useRef(null)
  const availableTemplates = useMemo(
    () => globalProjectTemplates(templates?.length ? templates : FALLBACK_TEMPLATES),
    [templates],
  )

  const coreTemplates = availableTemplates.filter(template => template.source_app_id == null)
  const appTemplates = availableTemplates.filter(template => template.source_app_id != null)

  useEffect(() => {
    if (!open) return undefined
    const focusFrame = requestAnimationFrame(() => firstItemRef.current?.focus())
    function dismiss(event) {
      if (!rootRef.current?.contains(event.target)) setOpen(false)
    }
    function closeOnEscape(event) {
      if (event.key !== 'Escape') return
      setOpen(false)
      rootRef.current?.querySelector('[data-project-create-trigger]')?.focus()
    }
    document.addEventListener('pointerdown', dismiss)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      cancelAnimationFrame(focusFrame)
      document.removeEventListener('pointerdown', dismiss)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [open, view])

  async function choose(template) {
    if (busyKey) return
    setBusyKey(template.key)
    setError('')
    try {
      await onCreate?.(template)
      setOpen(false)
    } catch (cause) {
      setError(cause?.message || 'Could not create that project.')
    } finally {
      setBusyKey(null)
    }
  }

  async function importRepository(event) {
    event.preventDefault()
    if (busyKey || !repository.trim()) return
    setBusyKey('github')
    setError('')
    try {
      await onImportGithub?.({
        repository: repository.trim(),
        name: projectName.trim() || null,
      })
      setOpen(false)
      setView('types')
      setRepository('')
      setProjectName('')
    } catch (cause) {
      setError(cause?.message || 'Could not import that repository.')
    } finally {
      setBusyKey(null)
    }
  }

  async function openSources() {
    setView('sources')
    setError('')
    setSourcesLoading(true)
    try {
      setSources(await jsonOrThrow(
        await api.projects.importSources(), 'Existing work failed:',
      ))
    } catch (cause) {
      setError(cause?.message || 'Could not load existing work.')
    } finally {
      setSourcesLoading(false)
    }
  }

  async function importSource(source) {
    if (busyKey) return
    const key = `${source.kind}:${source.id}`
    setBusyKey(key)
    setError('')
    try {
      await onImportSource?.(source)
      setOpen(false)
      setView('types')
    } catch (cause) {
      setError(cause?.message || 'Could not import that work.')
    } finally {
      setBusyKey(null)
    }
  }

  const artifactSources = Array.isArray(sources?.artifacts) ? sources.artifacts : []
  const appSources = Array.isArray(sources?.apps) ? sources.apps : []

  return (
    <div ref={rootRef} className={`project-create-menu ${className}`.trim()}>
      <button
        type="button"
        className={`project-create-menu__trigger${showLabel ? ' project-create-menu__trigger--label' : ''}`}
        data-project-create-trigger=""
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => { setOpen(current => !current); setView('types'); setError('') }}
      >
        <Plus width={20} height={20} aria-hidden="true" />{showLabel && <span>New project</span>}
      </button>
      {open && (
        <div className={`project-create-menu__popover project-create-menu__popover--${align}`} role={view === 'types' ? 'menu' : 'dialog'} onKeyDown={event => {
          if (view !== 'types' || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
          const items = [...event.currentTarget.querySelectorAll('[role=menuitem]:not(:disabled)')]
          if (!items.length) return
          event.preventDefault()
          const index = items.indexOf(document.activeElement)
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
          items[next].focus()
        }} aria-label={view === 'types' ? 'Project types' : view === 'sources' ? 'Open existing work' : 'Import GitHub repository'}>
          {view === 'types' ? <>
          <div className="project-create-menu__heading">New project</div>
          {coreTemplates.map((template, index) => (
            <button
              key={template.key}
              ref={index === 0 ? firstItemRef : null}
              type="button"
              role="menuitem"
              disabled={busyKey != null}
              onClick={() => void choose(template)}
            >
              <span className="project-create-menu__icon" aria-hidden="true">
                <ProjectTypeIcon value={template} size={19} />
              </span>
              <span>
                <strong>{busyKey === template.key ? 'Creating…' : template.name}</strong>
                {template.description && <small>{template.description}</small>}
              </span>
            </button>
          ))}
          {onImportGithub && <button
            disabled={busyKey != null}
            type="button"
            role="menuitem"
            onClick={() => { setView('github'); setError('') }}
          >
            <span className="project-create-menu__icon project-create-menu__icon--github" aria-hidden="true"><Folder width={19} height={19} /></span>
            <span><strong>Import from GitHub</strong><small>Bring a repository into a private local workspace.</small></span>
          </button>}
          {appTemplates.length > 0 && <div className="project-create-menu__heading">From Project apps</div>}
          {appTemplates.map(template => <button key={template.key} type="button" role="menuitem" disabled={busyKey != null} onClick={() => void choose(template)}>
            <span className="project-create-menu__icon" aria-hidden="true"><ProjectTypeIcon value={template} size={19} /></span>
            <span><strong>{busyKey === template.key ? 'Creating…' : template.name}</strong><small>{template.source_app_name}{template.description ? ` · ${template.description}` : ''}</small></span>
          </button>)}
          {onImportSource && <div className="project-create-menu__divider" />}
          {onImportSource && <button
            type="button"
            role="menuitem"
            disabled={busyKey != null}
            onClick={() => void openSources()}
          >
            <span className="project-create-menu__icon" aria-hidden="true"><Folder width={19} height={19} /></span>
            <span><strong>Open existing</strong><small>Edit a locally built app’s live source, or copy a Page into a Project.</small></span>
          </button>}
          </> : view === 'sources' ? <div className="project-create-menu__sources">
            <button ref={firstItemRef} type="button" className="project-create-menu__back" onClick={() => { setView('types'); setError('') }}><ArrowLeft width={16} height={16} /> Project types</button>
            <div className="project-create-menu__heading">Open existing</div>
            <p className="project-create-menu__privacy">Apps open their live source. Pages become independent editable copies.</p>
            {sourcesLoading ? <p className="project-create-menu__state" role="status">Loading your work…</p> : (
              <div className="project-create-menu__source-list">
                {artifactSources.length > 0 && <>
                  <h3>Pages</h3>
                  {artifactSources.map(source => <button key={`artifact:${source.id}`} type="button" disabled={busyKey != null} onClick={() => void importSource(source)}>
                    <span className="project-create-menu__icon" aria-hidden="true"><FileDocument width={18} height={18} /></span>
                    <span><strong>{busyKey === `artifact:${source.id}` ? 'Importing…' : source.name}</strong><small>{source.description || `Version ${source.current_version}`}</small></span>
                  </button>)}
                </>}
                {appSources.length > 0 && <>
                  <h3>Apps</h3>
                  {appSources.map(source => <button key={`app:${source.id}`} type="button" disabled={busyKey != null} onClick={() => void importSource(source)}>
                    <span className="project-create-menu__icon project-create-menu__icon--app" aria-hidden="true"><FileCode width={18} height={18} /></span>
                    <span><strong>{busyKey === `app:${source.id}` ? 'Opening…' : source.name}</strong><small>{source.description || 'Edit the installed app source'}</small></span>
                  </button>)}
                </>}
                {!sourcesLoading && artifactSources.length === 0 && appSources.length === 0 && !error && <p className="project-create-menu__state">No Pages or locally built apps yet.</p>}
              </div>
            )}
          </div> : <form className="project-create-menu__import" onSubmit={importRepository}>
            <button type="button" className="project-create-menu__back" onClick={() => { setView('types'); setError('') }}><ArrowLeft width={16} height={16} /> Project types</button>
            <div className="project-create-menu__heading">Import from GitHub</div>
            <label>Repository
              <input ref={firstItemRef} value={repository} onChange={event => setRepository(event.target.value)} placeholder="owner/repository" autoComplete="off" disabled={busyKey != null} />
            </label>
            <label>Project name <span>optional</span>
              <input value={projectName} onChange={event => setProjectName(event.target.value)} placeholder="Uses the repository name" maxLength={256} disabled={busyKey != null} />
            </label>
            <p className="project-create-menu__privacy">Imports a private local copy. Nothing is pushed or published.</p>
            <button type="submit" className="project-create-menu__submit" disabled={busyKey != null || !repository.trim()}>{busyKey ? 'Importing…' : 'Import repository'}</button>
          </form>}
          {error && <div role="alert" className="project-create-menu__error"><p>{error}</p>{view === 'sources' && <button type="button" onClick={() => void openSources()}>Try again</button>}</div>}
        </div>
      )}
    </div>
  )
}
