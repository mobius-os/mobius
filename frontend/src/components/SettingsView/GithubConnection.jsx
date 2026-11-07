/* The instance's GitHub account row in Settings › Accounts: avatar and login, then connect with a device code, add or remove private-repo access, or disconnect. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUpRight, ChevronDown } from '@openai/apps-sdk-ui/components/Icon'
import useAuthedImageUrl from '../../hooks/useAuthedImageUrl.js'
import {
  cancelGithubSignIn,
  disconnectGithub,
  githubStatus,
  hasFullPrAccess,
  hasPrivateRepoAccess,
  startGithubSignIn,
  unknownGithubStatus,
  waitForGithubSignIn,
} from '../../lib/githubConnection.js'

function GithubMark() {
  return <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M12 .3a12 12 0 0 0-3.8 23.4c.6.1.8-.3.8-.6v-2.2c-3.3.7-4-1.4-4-1.4-.5-1.4-1.3-1.8-1.3-1.8-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-5.9 0-1.3.5-2.4 1.2-3.2-.1-.3-.5-1.5.1-3.2 0 0 1-.3 3.3 1.2a11.4 11.4 0 0 1 6 0c2.3-1.5 3.3-1.2 3.3-1.2.7 1.7.3 2.9.1 3.2.8.8 1.2 1.9 1.2 3.2 0 4.6-2.8 5.6-5.5 5.9.4.4.8 1.1.8 2.2v3.3c0 .3.2.7.8.6A12 12 0 0 0 12 .3Z" /></svg>
}

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
  const queryClient = useQueryClient()
  // The last confirmed account paints at once, from the persisted cache after
  // a reload, while the read Settings starts on opening confirms it. A read
  // that fails is shown as such rather than as that older account.
  const account = useQuery(githubStatus.options).data
  const [unreadable, setUnreadable] = useState(null)
  const conn = unreadable || account || { state: 'checking' }
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

  // Resolves the newest status, or null when an account change superseded this read.
  const refresh = useCallback(async () => {
    try {
      const next = await githubStatus.refresh(queryClient)
      if (next) setUnreadable(null)
      return next
    } catch (error) {
      const next = unknownGithubStatus(error)
      setUnreadable(next)
      return next
    }
  }, [queryClient])

  useEffect(() => () => waitRef.current?.abort(), [])

  // Closing the disclosure cancels destructive intent, not device-code sign-in.
  useEffect(() => {
    if (!expanded || !active) setConfirmDisconnect(false)
  }, [expanded, active])

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
      const intent = next ? permissionIntentRef.current : null
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
      if (!disposed && next?.attempt && !waitRef.current) void waitFor(next.attempt)
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
      if (attemptId) await githubStatus.change(queryClient, () => cancelGithubSignIn(attemptId, { signal: controller.signal }))
    } catch (error) {
      if (controller.signal.aborted) return
      setMessage(error.message)
    }
    const next = await refresh()
    if (controller.signal.aborted) return
    setBusy(false)
    if (next?.attempt) {
      void waitFor(next.attempt, controller)
    } else {
      waitRef.current = null
      // Null: an account change started since, and the status is read again after it.
      if (next?.state !== 'unknown') setSignIn(null)
    }
  }, [signIn, refresh, waitFor, queryClient])

  const disconnect = useCallback(async () => {
    setBusy(true)
    setMessage('')
    try {
      await githubStatus.change(queryClient, disconnectGithub)
    } catch (error) {
      setMessage(error.message)
    }
    // Disconnect is idempotent; the fresh status is the truth either way.
    await refresh()
    setBusy(false)
    setConfirmDisconnect(false)
  }, [refresh, queryClient])

  const connected = conn.state === 'connected'
  const privateAccess = connected && hasPrivateRepoAccess(conn.scopes)
  const needsReconnect = connected && !hasFullPrAccess(conn.scopes)

  const note = message ? <p className="settings-message settings-message--error" role="status">{message}</p> : null
  const button = (label, onClick, extra = '') => (
    <button type="button" className={`settings__btn settings__btn--sm ${extra || 'settings__btn--outline'}`} disabled={busy} onClick={onClick}>{label}</button>
  )
  let panel
  if (signIn === 'starting') {
    panel = <p className="settings-item-panel__text" role="status">Starting GitHub sign-in…</p>
  } else if (signIn) {
    panel = <SignInCode attempt={signIn} retrying={retrying} cancelling={busy} unconfirmed={conn.state === 'unknown'} message={message} onCancel={cancel} />
  } else if (conn.state === 'unknown') {
    panel = (
      <div className="settings-github__panel">
        <p className="settings-item-panel__text">{conn.message}</p>
        <div className="settings-actions">{button('Check again', () => { void refresh() })}</div>
      </div>
    )
  } else if (!connected) {
    panel = conn.signInAvailable ? (
      <div className="settings-github__panel">
        <p className="settings-item-panel__text">Used to send changes, open pull requests, and follow reviews as you.</p>
        <label className="settings-github__check">
          <input type="checkbox" checked={includePrivate} onChange={event => setIncludePrivate(event.target.checked)} />
          Include private repositories
        </label>
        <div className="settings-actions">
          <button type="button" className="settings__btn settings__btn--sm" onClick={() => start(includePrivate)}>Connect GitHub</button>
        </div>
        {note}
      </div>
    ) : <p className="settings-item-panel__text">GitHub sign-in is not configured for this Möbius instance.</p>
  } else if (confirmDisconnect) {
    panel = (
      <div className="settings-github__panel">
        <p className="settings-item-panel__text">Disconnect GitHub? Saved drafts and review history stay in your apps.</p>
        <div className="settings-actions">
          {button('Cancel', () => setConfirmDisconnect(false))}
          {button(busy ? 'Disconnecting…' : 'Disconnect', disconnect, 'settings__btn--danger')}
        </div>
        {note}
      </div>
    )
  } else {
    panel = (
      <div className="settings-github__panel">
        {needsReconnect ? (
          <p className="settings-item-panel__text">This older connection lacks access Möbius now needs to send changes. Disconnect, then connect again.</p>
        ) : null}
        {!needsReconnect && !privateAccess ? <p className="settings-item-panel__text">Private access applies to all private repositories your GitHub account can access, not just one Project.</p> : null}
        <div className="settings-actions">
          {!needsReconnect && !privateAccess ? button('Enable private repositories', () => start(true, 'add-private')) : null}
          {privateAccess && !removePrivateOpen ? button('Remove private access…', () => setRemovePrivateOpen(true)) : null}
          {button('Disconnect…', () => setConfirmDisconnect(true), 'settings__btn--danger')}
        </div>
        {privateAccess && removePrivateOpen ? <div className="settings-github__permission-guide">
          <p className="settings-item-panel__text">Removing the saved connection here does not revoke GitHub’s permission. First revoke this app under GitHub’s Authorized OAuth Apps, then reconnect with public repositories only. GitHub actions in Möbius will pause until you reconnect.</p>
          <div className="settings-actions">
            <a className="settings__btn settings__btn--sm settings__btn--outline" href="https://github.com/settings/applications" target="_blank" rel="noopener noreferrer">Open GitHub authorizations</a>
            {button('I revoked it — reconnect public only', () => start(false, 'remove-private'))}
            {button('Cancel', () => setRemovePrivateOpen(false))}
          </div>
        </div> : null}
        {note}
      </div>
    )
  }

  // The avatar's address names the login, so it and its cached copies follow
  // the connected account; reopening Settings shows it at once, and until the
  // first read settles a quiet placeholder stands in.
  const avatar = useAuthedImageUrl(
    connected && conn.login ? `/github/avatar/${encodeURIComponent(conn.login)}` : null,
    connected ? conn.login : '',
  )
  const avatarPending = conn.state === 'checking' || avatar.status === 'loading'
  const profileUrl = connected && conn.login ? `https://github.com/${encodeURIComponent(conn.login)}` : null
  const exception = connected && needsReconnect
    ? <span className="settings-badge settings-badge--warning">Reconnect needed</span>
    : conn.state === 'unknown'
      ? <span className="settings-badge settings-badge--warning">Unavailable</span>
      : conn.state === 'disconnected' && conn.signInAvailable && !signIn
        ? <span className="settings-badge settings-badge--accent settings-badge--plain">Connect</span>
        : null
  const subtitle = connected
    ? conn.login
    : conn.state === 'checking' ? 'Checking…' : conn.state === 'unknown' ? 'Status unavailable' : 'Not connected'

  return (
    <div
      className={`settings-github${attention ? ' settings-setup-target' : ''}`}
      id="settings-github"
      ref={focusRef}
      tabIndex={-1}
    >
      <button type="button" className="settings-item" aria-expanded={expanded} aria-controls="settings-github-detail" onClick={onToggle}>
        <span className="settings-item__icon settings-item__icon--round settings-github__mark" aria-hidden="true">
          {avatar.url ? <img src={avatar.url} alt="" /> : avatarPending ? null : <GithubMark />}
        </span>
        <span className="settings-item__body">
          <span className="settings-item__title">GitHub</span>
          <span className="settings-item__sub">{subtitle}</span>
        </span>
        {exception}
        <ChevronDown className="settings-item__chevron" width={18} height={18} aria-hidden="true" />
      </button>
      {expanded && <section className="settings-item-panel settings-github__detail" id="settings-github-detail" aria-label="GitHub connection">
        {connected && !signIn && !confirmDisconnect && <dl className="settings-kv">
          <div className="settings-kv__row">
            <dt>Account</dt>
            <dd><a className="settings-link" href={profileUrl} target="_blank" rel="noopener noreferrer">{conn.login}<ArrowUpRight width={14} height={14} aria-hidden="true" /></a></dd>
          </div>
          <div className="settings-kv__row">
            <dt>Private repositories</dt>
            <dd>{privateAccess
              ? <span className="settings-badge settings-badge--success">Active</span>
              : <span className="settings-badge">Off</span>}</dd>
          </div>
        </dl>}
        {panel}
      </section>}
    </div>
  )
}
