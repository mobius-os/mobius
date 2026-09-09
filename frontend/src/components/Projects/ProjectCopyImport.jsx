/* Recipients review an external project's files before explicitly saving a separate local copy. */
import { useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { X } from '@openai/apps-sdk-ui/components/Icon'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { copyByteLabel, projectCopyRequest } from '../../lib/projectCopies.js'
import './ProjectCopy.css'

export default function ProjectCopyImport({ url, onImported, onClose }) {
  const containerRef = useRef(null)
  const [busy, setBusy] = useState(false)
  const [saveStarted, setSaveStarted] = useState(false)
  const recoveryRef = useRef(null)
  const [error, setError] = useState('')
  const [name, setName] = useState(null)
  useDialogFocus({ containerRef, onClose, closeOnEscape: !busy })
  const preview = useQuery({ queryKey: ['project-copy-import', url], queryFn: ({ signal }) => projectCopyRequest('/preview', { method: 'POST', body: { url }, signal }), retry: false, gcTime: 0 })
  const source = (() => { try { return new URL(url).origin } catch { return 'Unknown address' } })()
  async function save(event) {
    event.preventDefault()
    if (busy || !preview.data) return
    setBusy(true); setSaveStarted(true); setError('')
    if (!recoveryRef.current || recoveryRef.current.digest !== preview.data.digest) {
      recoveryRef.current = { digest: preview.data.digest, id: crypto.randomUUID() }
    }
    let project
    try {
      project = await projectCopyRequest('/import', { method: 'POST', body: { url, digest: preview.data.digest, recovery_request_id: recoveryRef.current.id, name: (name ?? preview.data.name).trim() } })
    } catch (cause) { setError(cause.message); setBusy(false); if (cause.status === 409) await preview.refetch(); return }
    // Saving succeeded even if subsequent workspace navigation fails; never invite a duplicate import.
    onImported(project)
  }
  return createPortal(<div className="project-copy-overlay" onPointerDown={event => { if (event.target === event.currentTarget && !busy) onClose() }}>
    <section ref={containerRef} className="project-copy project-copy-dialog" role="dialog" aria-modal="true" aria-labelledby="project-copy-import-title" tabIndex={-1}>
      <header><h2 id="project-copy-import-title">Save your own copy</h2><button aria-label="Close" disabled={busy} onClick={onClose}><X width={20} height={20} /></button></header>
      <p>From <strong className="project-copy__origin">{source}</strong></p>
      {preview.isPending && <p role="status">Reviewing the shared files…</p>}
      {preview.isError && <div role="alert"><p className="project-copy__error">{preview.error.message}</p><button onClick={() => preview.refetch()}>Try again</button></div>}
      {preview.data && <form onSubmit={save}>
        <label htmlFor="project-copy-name">Name for your copy</label><input id="project-copy-name" required maxLength={255} disabled={busy || saveStarted} value={name ?? preview.data.name} onChange={event => setName(event.target.value)} />
        <details><summary>{preview.data.files.length} files · {copyByteLabel(preview.data.total_bytes)}</summary><div className="project-copy__files">{preview.data.files.map(file => <div key={file.path}><span>{file.path}</span><small>{copyByteLabel(file.size)}</small></div>)}</div></details>
        {preview.data.copy_warning && <p className="project-copy__warning">{preview.data.copy_warning}</p>}
        <p>You’ll get a new project, separate from the original. Changes won’t be shared in either direction. No GitHub account needed.</p>
        <p className="project-copy__boundary">Only save projects from someone you trust. Saving copies the files; it does not run or install them. Review them before building or running anything.</p>
        {error && <p className="project-copy__error" role="alert">{error} Try saving again to recover this same copy, without creating a duplicate.</p>}
        <div className="project-copy__actions"><button type="button" disabled={busy} onClick={onClose}>Cancel</button><button className="project-copy__primary" type="submit" disabled={busy || !(name ?? preview.data.name).trim()}>{busy ? 'Saving your copy…' : 'Save my copy'}</button></div>
      </form>}
    </section>
  </div>, document.body)
}
