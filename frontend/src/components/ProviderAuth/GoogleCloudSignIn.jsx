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
const OUTLINE = 'settings__btn settings__btn--sm settings__btn--outline'
const PRIMARY = 'settings__btn settings__btn--sm'

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

function StepIcon({ state, number }) {
  return (
    <span className={`gemini-step__n gemini-step__n--${state}`} aria-hidden="true">
      {state === 'done'
        ? <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 6.2 5 8.6 9.6 3.6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
        : number}
    </span>
  )
}

/** One numbered step of the sign-in, Codex-style: done, now or still to come. */
function Step({ state, number, title, children }) {
  return (
    <li className={`gemini-step gemini-step--${state}`} aria-current={state === 'now' ? 'step' : undefined}>
      <StepIcon state={state} number={number} />
      <div className="gemini-step__body">
        <span className="gemini-step__title">{title}</span>
        {children}
      </div>
    </li>
  )
}

function CodeStep({ link, onProjects, onCancel }) {
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
    <ol className="gemini-steps">
      <Step state="done" number={1} title="Approve in Google">
        <span className="pa__muted">
          Google’s page opened in a new tab. <a href={link} target="_blank" rel="noopener noreferrer">Open it again</a>
        </span>
      </Step>
      <Step state="now" number={2} title="Paste the code Google shows">
        <form className="gemini-step__form" onSubmit={submit}>
          <input
            className="pa__input" aria-label="Code from Google" autoComplete="off" spellCheck={false}
            value={code} onChange={(event) => setCode(event.target.value)} disabled={busy}
          />
          <div className="provider-connection__actions">
            <button type="submit" className={PRIMARY} disabled={busy || !code.trim()}>{busy ? 'Checking code…' : 'Continue'}</button>
            <button type="button" className={OUTLINE} disabled={busy} onClick={onCancel}>Cancel</button>
          </div>
          {error && <p className="pa__error" role="alert">{error}</p>}
        </form>
      </Step>
      <Step state="todo" number={3} title="Choose a project" />
    </ol>
  )
}

function ProjectChoice({ projects, project, setProject, disabled }) {
  return (
    <div className="gemini-projects" role="radiogroup" aria-label="Google Cloud project">
      {projects.map(row => (
        <label key={row.project_id} className={`gemini-project${row.project_id === project ? ' gemini-project--on' : ''}`}>
          <input
            type="radio" name="gemini-project" value={row.project_id} disabled={disabled}
            checked={row.project_id === project} onChange={() => setProject(row.project_id)}
          />
          <span>
            <span className="gemini-project__name">{row.name}</span>
            {row.name !== row.project_id && <span className="gemini-project__id">{row.project_id}</span>}
          </span>
        </label>
      ))}
    </div>
  )
}

function ProjectStep({ projects, onConnected, onCancel }) {
  const queryClient = useQueryClient()
  const [typing, setTyping] = useState(projects.length === 0)
  const [project, setProject] = useState(projects[0]?.project_id || '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const chosen = projects.find(row => row.project_id === project.trim())

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    const response = await api.auth.provider.geminiGoogleCloud.project(project.trim(), chosen?.name).catch(() => null)
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
    <ol className="gemini-steps">
      <Step state="done" number={1} title="Signed in to Google" />
      <Step state="now" number={3} title="Choose the project Gemini bills to">
        <form className="gemini-step__form" onSubmit={submit}>
          {typing ? (
            <input
              className="pa__input" aria-label="Google Cloud project ID" autoComplete="off" spellCheck={false}
              placeholder="my-project-123" value={project} onChange={(event) => setProject(event.target.value)} disabled={busy}
            />
          ) : <ProjectChoice projects={projects} project={project} setProject={setProject} disabled={busy} />}
          <span className="pa__muted">
            The project needs billing and the Agent Platform API turned on.
            {projects.length > 0 && (
              <>
                {' '}
                <button type="button" className="gemini-connection__link" onClick={() => setTyping(!typing)}>
                  {typing ? 'Choose from your projects' : 'Type a project ID instead'}
                </button>
              </>
            )}
          </span>
          <div className="provider-connection__actions">
            <button type="submit" className={PRIMARY} disabled={busy || !project.trim()}>{busy ? 'Connecting…' : 'Connect'}</button>
            <button type="button" className={OUTLINE} disabled={busy} onClick={onCancel}>Cancel</button>
          </div>
          {error && <p className="pa__error" role="alert">{error}</p>}
        </form>
      </Step>
    </ol>
  )
}

/**
 * The Google Cloud sign-in: the connectors' Google-account sign-in (the Cloud
 * SDK's public client and Google's code page), so Möbius ships no gcloud and
 * needs no Google app of its own. Sign in with Google, paste the code, then
 * choose the project. ``onCancel`` returns to the caller; without it, Cancel
 * starts over.
 */
export default function GoogleCloudSignIn({ onConnected, onCancel }) {
  const [link, setLink] = useState('')
  const [projects, setProjects] = useState(null)
  const [prepared, prepare] = usePreparedSignInLink(Boolean(link))

  function cancel() {
    setLink('')
    setProjects(null)
    if (onCancel) onCancel()
    else void prepare()
  }

  function openSignIn() {
    if (prepared.status === 'failed') {
      void prepare()
      return
    }
    window.open(prepared.url, '_blank', 'noopener')
    setLink(prepared.url)
  }

  if (projects) return <ProjectStep projects={projects} onConnected={onConnected} onCancel={cancel} />
  if (link) return <CodeStep link={link} onProjects={setProjects} onCancel={cancel} />
  const button = SIGN_IN_BUTTON[prepared.status]
  return (
    <div className="gemini-signin">
      <button type="button" className="pa__btn pa__btn--with-spinner gemini-signin__button" disabled={button.busy} aria-busy={button.busy} onClick={openSignIn}>
        {button.busy && <progress className="pa__spinner" aria-hidden="true" />}
        {button.label}
      </button>
      <span className="pa__muted">Billed to a Google Cloud project you choose. Google AI Pro and Ultra include monthly Cloud credits.</span>
      {prepared.error && <p className="pa__error" role="alert">{prepared.error}</p>}
      {onCancel && <div><button type="button" className={OUTLINE} onClick={onCancel}>Cancel</button></div>}
    </div>
  )
}
