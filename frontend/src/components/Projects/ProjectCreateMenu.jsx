import { useEffect, useMemo, useRef, useState } from 'react'
import Plus from 'lucide-react/dist/esm/icons/plus.mjs'
import FolderGit from 'lucide-react/dist/esm/icons/folder-git-2.mjs'
import ArrowLeft from 'lucide-react/dist/esm/icons/arrow-left.mjs'
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
  className = '',
  align = 'end',
  label = 'Create project',
}) {
  const [open, setOpen] = useState(false)
  const [busyKey, setBusyKey] = useState(null)
  const [error, setError] = useState('')
  const [view, setView] = useState('types')
  const [repository, setRepository] = useState('')
  const [projectName, setProjectName] = useState('')
  const rootRef = useRef(null)
  const firstItemRef = useRef(null)
  const availableTemplates = useMemo(
    () => globalProjectTemplates(templates?.length ? templates : FALLBACK_TEMPLATES),
    [templates],
  )

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

  return (
    <div ref={rootRef} className={`project-create-menu ${className}`.trim()}>
      <button
        type="button"
        className="project-create-menu__trigger"
        data-project-create-trigger=""
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => { setOpen(current => !current); setView('types'); setError('') }}
      >
        <Plus size={21} strokeWidth={2} />
      </button>
      {open && (
        <div className={`project-create-menu__popover project-create-menu__popover--${align}`} role={view === 'types' ? 'menu' : 'dialog'} aria-label={view === 'types' ? 'Project types' : 'Import GitHub repository'}>
          {view === 'types' ? <>
          <div className="project-create-menu__heading">New project</div>
          {onImportGithub && <button
            ref={firstItemRef}
            type="button"
            role="menuitem"
            onClick={() => { setView('github'); setError('') }}
          >
            <span className="project-create-menu__icon project-create-menu__icon--github" aria-hidden="true"><FolderGit size={19} /></span>
            <span><strong>Import from GitHub</strong><small>Bring a repository into a private local workspace.</small></span>
          </button>}
          {availableTemplates.map((template, index) => (
            <button
              key={template.key}
              ref={!onImportGithub && index === 0 ? firstItemRef : null}
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
          </> : <form className="project-create-menu__import" onSubmit={importRepository}>
            <button type="button" className="project-create-menu__back" onClick={() => { setView('types'); setError('') }}><ArrowLeft size={15} /> Project types</button>
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
          {error && <p role="alert">{error}</p>}
        </div>
      )}
    </div>
  )
}
