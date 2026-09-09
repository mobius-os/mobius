/* Owners review selected files before creating an expiring independent-copy link. */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Copy } from '@openai/apps-sdk-ui/components/Icon'
import { copyByteLabel, copyDate, projectCopyRequest, selectedCopyPaths } from '../../lib/projectCopies.js'
import './ProjectCopy.css'

export default function ProjectCopyPanel({ project }) {
  const [selection, setSelection] = useState(null)
  const [created, setCreated] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [confirmStop, setConfirmStop] = useState('')
  const prefix = `/projects/${encodeURIComponent(project.id)}`
  const preview = useQuery({ queryKey: ['project-copy', project.id, 'preview'], queryFn: ({ signal }) => projectCopyRequest(`${prefix}/preview`, { signal }), retry: false })
  const shares = useQuery({ queryKey: ['project-copy', project.id, 'shares'], queryFn: ({ signal }) => projectCopyRequest(`${prefix}/shares`, { signal }), retry: false })
  const files = preview.data?.files || []
  const selected = selectedCopyPaths(selection, preview.data)
  const selectedBytes = files.filter(file => selected.includes(file.path)).reduce((sum, file) => sum + file.size, 0)

  function toggle(path) {
    setSelection({ digest: preview.data.digest, paths: selected.includes(path) ? selected.filter(item => item !== path) : [...selected, path] })
  }
  async function create() {
    if (busy || !selected.length) return
    setBusy('create'); setError(''); setNotice('')
    try {
      const next = await projectCopyRequest(`${prefix}/shares`, { method: 'POST', body: { paths: selected, digest: preview.data.digest } })
      setCreated(next)
      await shares.refetch()
      setNotice('Link ready. Only the files you selected are included.')
    } catch (cause) { setError(cause.message); if (cause.status === 409) await preview.refetch() }
    finally { setBusy('') }
  }
  async function copy() {
    try { await navigator.clipboard.writeText(created.copy_url); setNotice('Link copied.') }
    catch { setNotice('Select and copy the link below. Your browser did not allow automatic copying.') }
  }
  async function stop(id) {
    if (busy) return
    setBusy(id); setError('')
    try {
      await projectCopyRequest(`/shares/${encodeURIComponent(id)}`, { method: 'DELETE' })
      if (created?.id === id) setCreated(null)
      setConfirmStop(''); setNotice('Link stopped. Copies already saved remain with their recipients.')
      await shares.refetch()
    } catch (cause) { setError(cause.message) }
    finally { setBusy('') }
  }
  return <div className="project-copy">
    <section>
      {preview.isPending && <p role="status">Loading project files…</p>}
      {preview.isError && <div role="alert"><p>{preview.error.message}</p><button onClick={() => preview.refetch()}>Try again</button></div>}
      {preview.data && <>
        <p className="project-copy__boundary">Anyone with the link can keep a copy. Check files for private information.</p>
        {files.length ? <details><summary>Files · {selected.length} selected · {copyByteLabel(selectedBytes)}</summary>
          <div className="project-copy__files">{files.map(file => <label key={file.path}><input type="checkbox" checked={selected.includes(file.path)} disabled={!!busy} onChange={() => toggle(file.path)} /><span>{file.path}</span><small>{copyByteLabel(file.size)}</small></label>)}</div>
        </details> : <p>There are no files available to share.</p>}
        <button className="project-copy__primary" disabled={!!busy || !selected.length} onClick={create}>{busy === 'create' ? 'Creating link…' : 'Create copy link'}</button>
        <small>Files only—no chats or app data. Link expires in 7 days.</small>
      </>}
    </section>
    {created && <section className="project-copy__result"><h3>Your copy link</h3><p>Expires {copyDate(created.expires_at).toLocaleString()}.</p><label htmlFor="project-copy-link">Share this link</label><input id="project-copy-link" readOnly value={created.copy_url} onFocus={event => event.target.select()} /><button onClick={copy}><Copy width={16} height={16} /> Copy link</button></section>}
    {error && <p className="project-copy__error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {(shares.isPending || shares.isError || shares.data?.length > 0) && <section><h3>Copy links</h3>
      {shares.isPending && <p role="status">Loading links…</p>}
      {shares.isError && <div role="alert"><p>{shares.error.message}</p><button onClick={() => shares.refetch()}>Try again</button></div>}
      {(shares.data || []).map(share => {
        const ended = share.revoked_at || copyDate(share.expires_at).getTime() <= Date.now()
        return <div key={share.id} className="project-copy__share"><div><strong>Created {copyDate(share.created_at).toLocaleString()}</strong><small>{share.revoked_at ? 'Stopped' : ended ? 'Expired' : `Expires ${copyDate(share.expires_at).toLocaleString()}`}</small></div>
          {!ended && (confirmStop === share.id ? <div><p>Stop new copies from this link? Existing copies won’t change.</p><button disabled={!!busy} onClick={() => stop(share.id)}>Stop link</button><button disabled={!!busy} onClick={() => setConfirmStop('')}>Keep link</button></div> : <button disabled={!!busy} onClick={() => setConfirmStop(share.id)}>Stop sharing</button>)}
        </div>
      })}
    </section>}
  </div>
}
