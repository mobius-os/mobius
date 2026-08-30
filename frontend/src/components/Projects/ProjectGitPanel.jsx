import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import ArrowDown from 'lucide-react/dist/esm/icons/arrow-down.mjs'
import ArrowUp from 'lucide-react/dist/esm/icons/arrow-up.mjs'
import Check from 'lucide-react/dist/esm/icons/check.mjs'
import ExternalLink from 'lucide-react/dist/esm/icons/external-link.mjs'
import GitBranch from 'lucide-react/dist/esm/icons/git-branch.mjs'
import Github from 'lucide-react/dist/esm/icons/folder-git-2.mjs'
import RefreshCw from 'lucide-react/dist/esm/icons/refresh-cw.mjs'
import X from 'lucide-react/dist/esm/icons/x.mjs'
import { api, jsonOrThrow } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { remoteSyncActions, remoteSyncPresentation } from '../../lib/projectGit.js'
import ProjectIdentityIcon from './ProjectIdentityIcon.jsx'
import './ProjectGitPanel.css'

export default function ProjectGitPanel({ project, onClose }) {
  const cardRef = useRef(null)
  const closeRef = useRef(null)
  const queryClient = useQueryClient()
  const [repository, setRepository] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [pushReview, setPushReview] = useState(false)
  useDialogFocus({ containerRef: cardRef, initialFocusRef: closeRef, onClose })

  const statusQuery = useQuery({
    queryKey: ['projects', 'git', project.id, 'remote'],
    queryFn: async () => jsonOrThrow(
      await api.projects.remoteStatus(project.id), 'GitHub status failed:',
    ),
    refetchInterval: 15_000,
  })
  const status = statusQuery.data || null
  const summary = remoteSyncPresentation(status)
  const actions = remoteSyncActions(status)
  useEffect(() => { setPushReview(false) }, [status?.head, status?.ahead, status?.behind, status?.dirty])

  async function connect(event) {
    event.preventDefault()
    const target = repository.trim()
    if (!target || busy) return
    setBusy('connect'); setError(''); setNotice('')
    try {
      await jsonOrThrow(
        await api.projects.connectRemote(project.id, target), 'Connection failed:',
      )
      await statusQuery.refetch()
      setNotice('GitHub repository connected. Fetch to compare its branch.')
    } catch (cause) {
      setError(cause?.message || 'Could not connect that repository.')
    } finally { setBusy('') }
  }

  async function initialize() {
    if (busy) return
    setBusy('initialize'); setError(''); setNotice('')
    try {
      await jsonOrThrow(await api.projects.initGit(project.id), 'Versioning failed:')
      await Promise.all([
        statusQuery.refetch(),
        queryClient.invalidateQueries({ queryKey: ['projects', 'git', project.id] }),
      ])
      setNotice('Local versioning is ready. Commit a snapshot from Files, then connect a repository here.')
    } catch (cause) {
      setError(cause?.message || 'Could not start versioning for this project.')
    } finally { setBusy('') }
  }

  async function run(action) {
    if (busy || !status) return
    setBusy(action); setError(''); setNotice('')
    if (action !== 'push') setPushReview(false)
    try {
      const response = action === 'fetch'
        ? await api.projects.fetchRemote(project.id)
        : action === 'pull'
          ? await api.projects.pullRemote(project.id, status.head)
          : await api.projects.pushRemote(project.id, status.head)
      const next = await jsonOrThrow(response, `${action} failed:`)
      await statusQuery.refetch()
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['projects', 'files', project.id] }),
        queryClient.invalidateQueries({ queryKey: ['projects', 'git', project.id] }),
      ])
      setNotice(action === 'fetch' ? 'GitHub comparison refreshed.' : action === 'pull' ? 'Incoming commits applied by fast-forward.' : 'Reviewed commits pushed to GitHub.')
      if (action === 'push') setPushReview(false)
      return next
    } catch (cause) {
      setError(cause?.message || `Could not ${action} this project.`)
      return null
    } finally { setBusy('') }
  }

  return createPortal((
    <div className="project-git__overlay" onPointerDown={event => { if (event.target === event.currentTarget) onClose?.() }}>
      <aside ref={cardRef} className="project-git" role="dialog" aria-modal="true" aria-labelledby="project-git-title" tabIndex={-1}>
        <header className="project-git__head">
          <ProjectIdentityIcon project={project} size={38} />
          <div><p>Version control</p><h2 id="project-git-title">Publish {project.name}</h2></div>
          <button ref={closeRef} type="button" aria-label="Close GitHub panel" onClick={onClose}><X size={18} /></button>
        </header>

        <div className="project-git__body">
          {statusQuery.isLoading ? <p className="project-git__empty" role="status">Reading project history…</p> : statusQuery.isError ? <div className="project-git__empty" role="alert"><p>{statusQuery.error?.message || 'GitHub status is unavailable.'}</p><button type="button" onClick={() => statusQuery.refetch()}>Try again</button></div> : <>
            <section className={`project-git__summary is-${summary.tone}`}>
              <span>{summary.tone === 'ready' ? <Check size={16} /> : <GitBranch size={16} />}</span>
              <div><strong>{summary.title}</strong><p>{summary.copy}</p></div>
            </section>

            {!status?.available ? <section className="project-git__guidance"><h3>Start with local versioning</h3><p>Create a private project history first. You can review and commit the current files before anything is connected or published.</p><button type="button" disabled={!!busy} onClick={() => void initialize()}><GitBranch size={14} /> {busy === 'initialize' ? 'Starting…' : 'Start versioning'}</button></section> : !status.connected ? <section>
              <div className="project-git__section-head"><h3>Connect repository</h3><span>Owner only</span></div>
              <form className="project-git__connect" onSubmit={connect}>
                <label htmlFor="project-git-repository">GitHub repository</label>
                <div><Github size={16} /><input id="project-git-repository" value={repository} maxLength={200} placeholder="owner/repository" onChange={event => setRepository(event.target.value)} /></div>
                <button type="submit" disabled={busy === 'connect' || !repository.trim()}>{busy === 'connect' ? 'Connecting…' : 'Connect repository'}</button>
              </form>
              {!status.github_connected && <p className="project-git__boundary">GitHub sign-in is not connected yet. You can attach the repository now, but Fetch and Push stay locked until you connect GitHub in Contribute.</p>}
            </section> : <>
              <section>
                <div className="project-git__repo">
                  <span><Github size={17} /></span><div><strong>{status.repository}</strong><small><GitBranch size={12} /> {status.branch || 'Detached'}{status.head ? ` · ${status.head}` : ''}</small></div>
                  <a href={status.web_url} target="_blank" rel="noopener noreferrer" aria-label={`Open ${status.repository} on GitHub`}><ExternalLink size={15} /></a>
                </div>
                <div className="project-git__counts" aria-label="Synchronization counts">
                  <span><ArrowUp size={14} /><strong>{status.ahead || 0}</strong><small>outgoing</small></span>
                  <span><ArrowDown size={14} /><strong>{status.behind || 0}</strong><small>incoming</small></span>
                </div>
                <div className="project-git__actions">
                  <button type="button" disabled={!!busy || !actions.fetch} onClick={() => void run('fetch')}><RefreshCw size={14} className={busy === 'fetch' ? 'is-spinning' : ''} />{busy === 'fetch' ? 'Fetching…' : 'Fetch'}</button>
                  <button type="button" disabled={!!busy || !actions.pull} onClick={() => void run('pull')}><ArrowDown size={14} />{busy === 'pull' ? 'Pulling…' : 'Pull'}</button>
                  <button type="button" className="is-primary" disabled={!!busy || !actions.push} aria-expanded={pushReview} onClick={() => setPushReview(true)}><ArrowUp size={14} />{busy === 'push' ? 'Pushing…' : 'Review & push'}</button>
                </div>
                {pushReview && <div className="project-git__push-review" role="group" aria-label="Confirm GitHub push">
                  <div><strong>Publish {status.ahead} {status.ahead === 1 ? 'commit' : 'commits'}?</strong><p>Destination: <code>{status.repository}:{status.branch}</code>. Uncommitted files and generated build output stay local.</p></div>
                  <div><button type="button" disabled={!!busy} onClick={() => setPushReview(false)}>Cancel</button><button type="button" className="is-primary" disabled={!!busy} onClick={() => void run('push')}>{busy === 'push' ? 'Pushing…' : `Push ${status.ahead}`}</button></div>
                </div>}
              </section>

              {status.commits?.length > 0 && <section>
                <div className="project-git__section-head"><h3>Outgoing commits</h3><span>{status.commits.length}{status.ahead > status.commits.length ? ` of ${status.ahead}` : ''}</span></div>
                <div className="project-git__commits">{status.commits.map(commit => <div key={commit.id}><code>{commit.id}</code><span>{commit.subject}</span></div>)}</div>
                <p className="project-git__review-note">Only these committed snapshots are published. Uncommitted files and generated build output are never included.</p>
              </section>}
            </>}
          </>}
          {notice && <p className="project-git__notice" role="status">{notice}</p>}
          {error && <p className="projects-error" role="alert">{error}</p>}
        </div>
      </aside>
    </div>
  ), document.body)
}
