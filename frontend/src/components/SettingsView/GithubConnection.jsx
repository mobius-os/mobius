/* The instance's GitHub account row in the Settings Accounts card: connect with a device code, add private-repo access, or disconnect. */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  cancelGithubSignIn,
  disconnectGithub,
  fetchGithubStatus,
  hasFullPrAccess,
  hasPrivateRepoAccess,
  startGithubSignIn,
  waitForGithubSignIn,
} from '../../lib/githubConnection.js'

function SignInCode({ attempt, retrying, cancelling, unconfirmed, message, onCancel }) {
  const [copied, setCopied] = useState(false)
  const copy = () => navigator.clipboard?.writeText(attempt.userCode).then(() => setCopied(true), () => {})
  return (
    <div className="codex-auth">
      <p className="pa__muted">Copy this one-time code, then open GitHub and paste it to continue.</p>
      <div className="codex-auth__device">
        <span className="codex-auth__code-copy">
          <code className="codex-auth__code" aria-label="GitHub device code" onClick={copy}>{attempt.userCode}</code>
          <button type="button" className="pa__btn pa__btn--sm codex-auth__copy-btn" onClick={copy}>
            {copied ? 'Copied' : 'Copy code'}
          </button>
        </span>
      </div>
      <div className="codex-auth__pending-actions">
        <p className="pa__muted codex-auth__waiting" role="status" aria-live="polite">
          {cancelling ? 'Cancelling GitHub sign-in…' : unconfirmed ? 'Sign-in status is unknown. Retry Cancel to check again.' : retrying ? 'GitHub is not responding. Retrying…' : 'Waiting for sign-in to complete…'}
        </p>
        <a className="pa__btn pa__btn--sm" href={attempt.verificationUri} target="_blank" rel="noopener noreferrer">Open GitHub</a>
        <button type="button" className="pa__btn pa__btn--sm" disabled={cancelling} onClick={onCancel}>Cancel</button>
      </div>
      {message ? <p className="pa__error" role="status">{message}</p> : null}
    </div>
  )
}

export default function GithubConnection({ active = true, focusRef, attention = false, expanded, onToggle, onExpand }) {
  const [conn, setConn] = useState({ state: 'checking' })
  // null, 'starting', or the attempt being waited on.
  const [signIn, setSignIn] = useState(null)
  const [retrying, setRetrying] = useState(false)
  const [message, setMessage] = useState('')
  const [includePrivate, setIncludePrivate] = useState(false)
  const [removePrivateOpen, setRemovePrivateOpen] = useState(false)
  const [confirmDisconnect, setConfirmDisconnect] = useState(false)
  const [busy, setBusy] = useState(false)
  const waitRef = useRef(null)
  const permissionIntentRef = useRef(null)

  const refresh = useCallback(async (options = {}) => {
    const next = await fetchGithubStatus(options)
    if (!options.signal?.aborted) setConn(next)
    return next
  }, [])

  useEffect(() => () => waitRef.current?.abort(), [])

  const waitFor = useCallback(async (attempt, controller = new AbortController()) => {
    if (controller.signal.aborted) return
    if (waitRef.current !== controller) waitRef.current?.abort()
    waitRef.current = controller
    setSignIn(attempt)
    onExpand()
    const result = await waitForGithubSignIn(attempt.attemptId, { signal: controller.signal, onRetrying: setRetrying })
    if (controller.signal.aborted) return
    waitRef.current = null
    setSignIn(null)
    setRetrying(false)
    const next = await refresh()
    if (result.status === 'complete') {
      setIncludePrivate(false)
      const intent = permissionIntentRef.current
      if (intent === 'remove-private' && hasPrivateRepoAccess(next.scopes)) {
        setMessage('GitHub still granted private-repository access. Revoke this app on GitHub before reconnecting with public access only.')
      } else if (intent === 'remove-private' && next.state === 'connected') {
        setRemovePrivateOpen(false)
      } else if (intent === 'add-private' && !hasPrivateRepoAccess(next.scopes)) {
        setMessage('GitHub did not grant private-repository access. Check the permissions you approved on GitHub.')
      }
    } else setMessage(result.message || '')
    permissionIntentRef.current = null
  }, [refresh, onExpand])

  const start = useCallback(async (privateRepos, intent = null) => {
    waitRef.current?.abort()
    const controller = new AbortController()
    waitRef.current = controller
    permissionIntentRef.current = intent
    setMessage('')
    setSignIn('starting')
    try {
      const attempt = await startGithubSignIn({ privateRepos, signal: controller.signal })
      await waitFor(attempt, controller)
    } catch (error) {
      if (controller.signal.aborted) return
      waitRef.current = null
      setSignIn(null)
      permissionIntentRef.current = null
      setMessage(error.message)
    }
  }, [waitFor])

  // Resume only when Settings opens, not when a local wait finishes or is cancelled.
  useEffect(() => {
    if (!active) return
    let disposed = false
    void refresh().then(next => {
      if (!disposed && next.attempt && !waitRef.current) void waitFor(next.attempt)
    })
    return () => { disposed = true }
  }, [active, refresh, waitFor])

  const cancel = useCallback(async () => {
    const attemptId = signIn?.attemptId
    waitRef.current?.abort()
    const controller = new AbortController()
    waitRef.current = controller
    setBusy(true)
    setRetrying(false)
    setMessage('')
    try {
      if (attemptId) await cancelGithubSignIn(attemptId, { signal: controller.signal })
    } catch (error) {
      if (controller.signal.aborted) return
      setMessage(error.message)
    }
    const next = await refresh({ signal: controller.signal })
    if (controller.signal.aborted) return
    setBusy(false)
    if (next.attempt) {
      void waitFor(next.attempt, controller)
    } else {
      waitRef.current = null
      if (next.state !== 'unknown') setSignIn(null)
    }
  }, [signIn, refresh, waitFor])

  const disconnect = useCallback(async () => {
    setBusy(true)
    setMessage('')
    try {
      await disconnectGithub()
    } catch (error) {
      setMessage(error.message)
    }
    // Disconnect is idempotent; the fresh status is the truth either way.
    await refresh()
    setBusy(false)
    setConfirmDisconnect(false)
  }, [refresh])

  const connected = conn.state === 'connected'
  const privateAccess = connected && hasPrivateRepoAccess(conn.scopes)
  const needsReconnect = connected && !hasFullPrAccess(conn.scopes)

  const note = message ? <p className="pa__error" role="status">{message}</p> : null
  const button = (label, onClick, extra = '') => (
    <button type="button" className={`pa__btn pa__btn--sm${extra}`} disabled={busy} onClick={onClick}>{label}</button>
  )
  let panel
  if (signIn === 'starting') {
    panel = <p className="pa__muted" role="status">Starting GitHub sign-in…</p>
  } else if (signIn) {
    panel = <SignInCode attempt={signIn} retrying={retrying} cancelling={busy} unconfirmed={conn.state === 'unknown'} message={message} onCancel={cancel} />
  } else if (conn.state === 'unknown') {
    panel = (
      <div className="provider-connection">
        <p className="pa__muted">{conn.message}</p>
        <div className="provider-connection__actions">{button('Check again', refresh)}</div>
      </div>
    )
  } else if (!connected) {
    panel = conn.signInAvailable ? (
      <div className="provider-connection">
        <p className="pa__muted">Used to send changes, open pull requests, and follow reviews as you.</p>
        <label className="settings-github__check">
          <input type="checkbox" checked={includePrivate} onChange={event => setIncludePrivate(event.target.checked)} />
          Include private repositories
        </label>
        <div className="provider-connection__actions">
          <button type="button" className="pa__btn" onClick={() => start(includePrivate)}>Connect GitHub</button>
        </div>
        {note}
      </div>
    ) : <p className="pa__muted">GitHub sign-in is not configured for this Möbius instance.</p>
  } else if (confirmDisconnect) {
    panel = (
      <div className="provider-connection">
        <p className="pa__muted">Disconnect GitHub? Saved drafts and review history stay in your apps.</p>
        <div className="provider-connection__actions">
          {button('Cancel', () => setConfirmDisconnect(false))}
          {button(busy ? 'Disconnecting…' : 'Disconnect', disconnect, ' provider-connection__disconnect')}
        </div>
        {note}
      </div>
    )
  } else {
    panel = (
      <div className="provider-connection">
        {needsReconnect ? (
          <p className="pa__muted">This older connection lacks access Möbius now needs to send changes. Disconnect, then connect again.</p>
        ) : null}
        {!needsReconnect && !privateAccess ? <p className="pa__muted">Private access applies to all private repositories your GitHub account can access, not just one Project.</p> : null}
        <div className="provider-connection__actions">
          {!needsReconnect && !privateAccess ? button('Enable private repositories', () => start(true, 'add-private')) : null}
          {privateAccess && !removePrivateOpen ? button('Remove private access…', () => setRemovePrivateOpen(true)) : null}
          {button('Disconnect…', () => setConfirmDisconnect(true))}
        </div>
        {privateAccess && removePrivateOpen ? <div className="settings-github__permission-guide">
          <p className="pa__muted">Removing the saved connection here does not revoke GitHub’s permission. First revoke this app under GitHub’s Authorized OAuth Apps, then reconnect with public repositories only. GitHub actions in Möbius will pause until you reconnect.</p>
          <div className="provider-connection__actions">
            <a className="pa__btn pa__btn--sm" href="https://github.com/settings/applications" target="_blank" rel="noopener noreferrer">Open GitHub authorizations</a>
            {button('I revoked it — reconnect public only', () => start(false, 'remove-private'))}
            {button('Cancel', () => setRemovePrivateOpen(false))}
          </div>
        </div> : null}
        {note}
      </div>
    )
  }

  const icon = <span className="settings__account-icon settings__account-icon--github" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 .3a12 12 0 0 0-3.8 23.4c.6.1.8-.3.8-.6v-2.2c-3.3.7-4-1.4-4-1.4-.5-1.4-1.3-1.8-1.3-1.8-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-5.9 0-1.3.5-2.4 1.2-3.2-.1-.3-.5-1.5.1-3.2 0 0 1-.3 3.3 1.2a11.4 11.4 0 0 1 6 0c2.3-1.5 3.3-1.2 3.3-1.2.7 1.7.3 2.9.1 3.2.8.8 1.2 1.9 1.2 3.2 0 4.6-2.8 5.6-5.5 5.9.4.4.8 1.1.8 2.2v3.3c0 .3.2.7.8.6A12 12 0 0 0 12 .3Z" /></svg></span>

  return (
    <div
      className={`settings-github${attention ? ' settings-setup-target' : ''}`}
      id="settings-github"
      ref={focusRef}
      tabIndex={-1}
    >
      <button type="button" className="settings__account-link" aria-expanded={expanded} aria-controls="settings-github-detail" onClick={onToggle}>
        {icon}
        <span className="settings__account-link-copy"><strong>GitHub</strong><small>{connected ? `${conn.login}${privateAccess ? ' · private repos' : ''}` : conn.state === 'checking' ? 'Checking…' : conn.state === 'unknown' ? 'Status unavailable' : 'Not connected'}</small></span>
        <span className="settings__account-link-arrow" aria-hidden="true">›</span>
      </button>
      {expanded && <section className="settings__account-detail" id="settings-github-detail" aria-label="GitHub connection">
        <div className="settings-account-detail-row"><span>Account</span><strong>{connected ? conn.login : 'Not connected'}</strong></div>
        <div className="settings-account-detail-row"><span>Status</span><strong>{connected ? needsReconnect ? 'Reconnect needed' : 'Connected' : conn.state === 'unknown' ? 'Unavailable' : 'Not connected'}</strong></div>
        {connected && <div className="settings-account-detail-row"><span>Repository access</span><strong>{privateAccess ? 'Public and private repositories' : 'Public repositories only'}</strong></div>}
        <div className="settings-github-detail__actions">{panel}</div>
      </section>}
    </div>
  )
}
