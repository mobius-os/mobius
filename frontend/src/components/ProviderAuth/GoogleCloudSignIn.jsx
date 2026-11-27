import { useCallback, useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { authQueries, modelQueries } from '../../hooks/queries.js'
import { detailToMessage } from '../../lib/errorDetail.js'

// A sign-in link stays valid for 15 minutes (connector_oauth._FLOW_TTL_SECONDS);
// an unopened link is replaced after this so the owner still has time to paste the code.
export const LINK_FRESH_MS = 5 * 60 * 1000

async function failureMessage(response, fallback) {
  const data = await response.json().catch(() => ({}))
  return detailToMessage(data.detail, fallback)
}

const START_FAILED = 'Could not start Google’s sign-in.'

/** What the Sign in button shows for each state of the prepared link. */
export const SIGN_IN_BUTTON = {
  preparing: { label: 'Sign in with Google', busy: true },
  ready: { label: 'Sign in with Google', busy: false },
  failed: { label: 'Try again', busy: false },
}

/**
 * Google's sign-in link, prepared before the owner clicks so the tab opens
 * straight on Google: a tab opened after a server round-trip is blocked as a
 * pop-up. The button waits with a spinner until the link is ready, and an
 * unopened link is replaced before it expires.
 */
function usePreparedSignInLink(opened) {
  const [prepared, setPrepared] = useState({ status: 'preparing' })

  const prepare = useCallback(async () => {
    setPrepared({ status: 'preparing' })
    const response = await api.auth.provider.geminiGoogleCloud.start().catch(() => null)
    if (!response?.ok) {
      setPrepared({ status: 'failed', error: response ? await failureMessage(response, START_FAILED) : START_FAILED })
      return
    }
    const { url } = await response.json()
    setPrepared({ status: 'ready', url })
  }, [])

  useEffect(() => { void prepare() }, [prepare])
  useEffect(() => {
    if (prepared.status !== 'ready' || opened) return undefined
    const timer = setTimeout(prepare, LINK_FRESH_MS)
    return () => clearTimeout(timer)
  }, [prepared, opened, prepare])

  return [prepared, prepare]
}

/** The sealed sign-in state Google echoes back; the server needs it with the code. */
export function signInState(url) {
  try {
    return new URL(url).searchParams.get('state') || ''
  } catch {
    return ''
  }
}

function CodeStep({ link, onProjects }) {
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    const response = await api.auth.provider.geminiGoogleCloud.code(code.trim(), signInState(link)).catch(() => null)
    setBusy(false)
    if (!response?.ok) {
      setError(response ? await failureMessage(response, 'Could not check the code.') : 'Could not check the code.')
      return
    }
    const { projects } = await response.json()
    onProjects(Array.isArray(projects) ? projects : [])
  }

  return (
    <form className="pa__form gemini-connection__form" onSubmit={submit}>
      <p className="pa__muted">
        Approve in the Google tab, then paste the code Google shows.{' '}
        <a href={link} target="_blank" rel="noopener noreferrer">Tab didn’t open?</a>
      </p>
      <label className="pa__field">
        <span className="pa__input-label">Verification code from Google</span>
        <input className="pa__input" autoComplete="off" spellCheck={false} value={code} onChange={(event) => setCode(event.target.value)} disabled={busy} />
      </label>
      <button type="submit" className="pa__btn" disabled={busy || !code.trim()}>
        {busy ? 'Checking code…' : 'Continue'}
      </button>
      {error && <p className="pa__error" role="alert">{error}</p>}
    </form>
  )
}

function ProjectStep({ projects, onConnected }) {
  const queryClient = useQueryClient()
  const [project, setProject] = useState(projects[0]?.project_id || '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    const response = await api.auth.provider.geminiGoogleCloud.project(project.trim()).catch(() => null)
    setBusy(false)
    if (!response?.ok) {
      setError(response ? await failureMessage(response, 'Could not finish the sign-in.') : 'Could not finish the sign-in.')
      return
    }
    void authQueries.provider.statuses.invalidate(queryClient)
    void modelQueries.registry.invalidate(queryClient)
    onConnected?.()
  }

  return (
    <form className="pa__form gemini-connection__form" onSubmit={submit}>
      <p className="pa__muted">Signed in. Choose the Google Cloud project Gemini bills to.</p>
      <label className="pa__field">
        <span className="pa__input-label">Google Cloud project</span>
        {projects.length > 0 ? (
          <select className="pa__input" value={project} onChange={(event) => setProject(event.target.value)} disabled={busy}>
            {projects.map(row => (
              <option key={row.project_id} value={row.project_id}>
                {row.name === row.project_id ? row.project_id : `${row.name} (${row.project_id})`}
              </option>
            ))}
          </select>
        ) : (
          <input className="pa__input" autoComplete="off" spellCheck={false} placeholder="my-project-123" value={project} onChange={(event) => setProject(event.target.value)} disabled={busy} />
        )}
      </label>
      <button type="submit" className="pa__btn" disabled={busy || !project.trim()}>
        {busy ? 'Connecting…' : 'Connect'}
      </button>
      {error && <p className="pa__error" role="alert">{error}</p>}
    </form>
  )
}

/**
 * The Google Cloud sign-in: the connectors' Google-account sign-in (the Cloud
 * SDK's public client and Google's code page), so Möbius ships no gcloud and
 * needs no Google app of its own. Link, pasted code, then the project.
 */
export default function GoogleCloudSignIn({ onConnected }) {
  const [link, setLink] = useState('')
  const [projects, setProjects] = useState(null)
  const [prepared, prepare] = usePreparedSignInLink(Boolean(link))

  function openSignIn() {
    if (prepared.status === 'failed') {
      void prepare()
      return
    }
    window.open(prepared.url, '_blank', 'noopener')
    setLink(prepared.url)
  }

  if (projects) return <ProjectStep projects={projects} onConnected={onConnected} />
  if (link) return <CodeStep link={link} onProjects={setProjects} />
  const button = SIGN_IN_BUTTON[prepared.status]
  return (
    <div className="pa__form gemini-connection__form">
      <p className="pa__muted">Google’s sign-in opens in a new tab. When you approve, Google shows a code to paste here.</p>
      <button type="button" className="pa__btn pa__btn--with-spinner" disabled={button.busy} aria-busy={button.busy} onClick={openSignIn}>
        {button.busy && <progress className="pa__spinner" aria-hidden="true" />}
        {button.label}
      </button>
      {prepared.error && <p className="pa__error" role="alert">{prepared.error}</p>}
    </div>
  )
}
