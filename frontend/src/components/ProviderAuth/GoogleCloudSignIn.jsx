import { useCallback, useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { authQueries, modelQueries } from '../../hooks/queries.js'
import { detailToMessage } from '../../lib/errorDetail.js'

// The server keeps a prepared sign-in for 15 minutes (gemini_google_cloud._LOGIN_SECONDS);
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
 * Google's sign-in link, prepared before the owner clicks: gcloud takes over
 * a second to start, and a tab opened only after that would start blank. The
 * button waits with a spinner until the link is ready. An unopened link is
 * replaced before the server stops its sign-in.
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

/** The Google Cloud sign-in: Google's own gcloud, so no app of Möbius's needs Google's review. */
export default function GoogleCloudSignIn({ onConnected }) {
  const queryClient = useQueryClient()
  const [link, setLink] = useState('')
  const [prepared, prepare] = usePreparedSignInLink(Boolean(link))
  const [code, setCode] = useState('')
  const [project, setProject] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  function openSignIn() {
    if (prepared.status === 'failed') {
      void prepare()
      return
    }
    window.open(prepared.url, '_blank', 'noopener')
    setLink(prepared.url)
  }

  async function finish(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    const response = await api.auth.provider.geminiGoogleCloud.finish(code.trim(), project.trim()).catch(() => null)
    setBusy(false)
    if (!response?.ok) {
      setError(response ? await failureMessage(response, 'Could not finish the sign-in.') : 'Could not finish the sign-in.')
      return
    }
    void authQueries.provider.statuses.invalidate(queryClient)
    void modelQueries.registry.invalidate(queryClient)
    onConnected?.()
  }

  if (!link) {
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
  return (
    <form className="pa__form gemini-connection__form" onSubmit={finish}>
      <p className="pa__muted">
        Approve in the Google tab, then paste the code Google shows.{' '}
        <a href={link} target="_blank" rel="noopener noreferrer">Tab didn’t open?</a>
      </p>
      <label className="pa__field">
        <span className="pa__input-label">Verification code from Google</span>
        <input className="pa__input" autoComplete="off" spellCheck={false} value={code} onChange={(event) => setCode(event.target.value)} disabled={busy} />
      </label>
      <label className="pa__field">
        <span className="pa__input-label">Google Cloud project ID</span>
        <input className="pa__input" autoComplete="off" spellCheck={false} placeholder="my-project-123" value={project} onChange={(event) => setProject(event.target.value)} disabled={busy} />
      </label>
      <button type="submit" className="pa__btn" disabled={busy || !code.trim() || !project.trim()}>
        {busy ? 'Signing in…' : 'Finish sign-in'}
      </button>
      {error && <p className="pa__error" role="alert">{error}</p>}
    </form>
  )
}
