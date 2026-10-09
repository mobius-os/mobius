import { useQueryClient } from '@tanstack/react-query'
import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react'
import {
  ArrowRotateCw,
  ArrowUpRight,
  Camera,
  CheckCircle,
  ChevronRight,
  ExternalLink,
  Lifesaver,
  Lock,
  Pencil,
  Plus,
  Trash,
  Warning,
} from '@openai/apps-sdk-ui/components/Icon'

import {
  IdentityRequestError,
  deploymentCanRecover,
  deploymentIsBuilding,
  deploymentNeedsTracking,
  deploymentPresentation,
  formatMembershipMonth,
  railwayAccountChanged,
  suggestRailwayRegion,
  waitForAccountLink,
} from './identity-contract.js'
import { IDENTITY_STYLES } from './identity-styles.js'
import {
  identityRequest,
  loadIdentity,
  publishIdentity,
  useAvatarSource,
  useIdentityQuery,
} from './identity-client.js'

function initials(profile) {
  const value = profile?.handle || profile?.email || 'M'
  return value
    .split(/\s+/)
    .map(part => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
}

function useDialog(onClose, blocked, initialFocusRef) {
  const dialogRef = useRef(null)
  const closeRef = useRef(onClose)
  const blockedRef = useRef(blocked)
  closeRef.current = onClose
  blockedRef.current = blocked

  useEffect(() => {
    const previousFocus = document.activeElement
    const dialog = dialogRef.current
    const focusTimer = setTimeout(() => {
      const target = initialFocusRef?.current
        || dialog?.querySelector('button:not(:disabled), input:not(:disabled)')
        || dialog
      target?.focus?.()
    }, 0)

    const onKeyDown = event => {
      if (event.key === 'Escape') {
        if (!blockedRef.current) {
          event.preventDefault()
          closeRef.current()
        }
        return
      }
      if (event.key !== 'Tab' || !dialog) return
      const focusable = [...dialog.querySelectorAll(
        'button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
      )]
      if (!focusable.length) {
        event.preventDefault()
        dialog.focus()
        return
      }
      const first = focusable[0]
      const last = focusable.at(-1)
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    dialog?.addEventListener('keydown', onKeyDown)
    return () => {
      clearTimeout(focusTimer)
      dialog?.removeEventListener('keydown', onKeyDown)
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  }, [initialFocusRef])

  return dialogRef
}

function GoogleMark() {
  return (
    <span className="id-provider-mark" aria-hidden="true">
      <svg viewBox="0 0 24 24">
        <path fill="#4285f4" d="M21.4 12.2c0-.6-.1-1.2-.2-1.8H12v3.5h5.3a4.5 4.5 0 0 1-2 2.9v2.3h3.2c1.8-1.7 2.9-4.2 2.9-6.9Z" />
        <path fill="#34a853" d="M12 21.7c2.6 0 4.9-.9 6.5-2.4l-3.2-2.3c-.9.6-2 .9-3.3.9-2.6 0-4.7-1.7-5.5-4H3.3v2.3a9.8 9.8 0 0 0 8.7 5.5Z" />
        <path fill="#fbbc05" d="M6.5 14a5.8 5.8 0 0 1 0-3.9V7.8H3.3a9.8 9.8 0 0 0 0 8.5L6.5 14Z" />
        <path fill="#ea4335" d="M12 6.1c1.4 0 2.7.5 3.7 1.4l2.8-2.8A9.4 9.4 0 0 0 12 2.2a9.8 9.8 0 0 0-8.7 5.6l3.2 2.3c.8-2.3 2.9-4 5.5-4Z" />
      </svg>
    </span>
  )
}

function AppleMark() {
  return (
    <span className="id-provider-mark" aria-hidden="true">
      <svg viewBox="0 0 24 24">
        <path
          fill="currentColor"
          d="M17.1 12.5c0-3.2 2.6-4.7 2.7-4.8a5.8 5.8 0 0 0-4.6-2.5c-1.9-.2-3.8 1.2-4.8 1.2s-2.5-1.1-4.2-1.1A6.2 6.2 0 0 0 1 8.5c-2.2 3.9-.6 9.5 1.6 12.6 1.1 1.5 2.3 3.2 4 3.2 1.6-.1 2.2-1 4.1-1s2.5 1 4.2 1c1.7 0 2.8-1.5 3.8-3.1a12.6 12.6 0 0 0 1.8-3.6 5.5 5.5 0 0 1-3.4-5.1ZM13.9 3.2A5.6 5.6 0 0 0 15.2-.8a5.6 5.6 0 0 0-3.7 1.9A5.3 5.3 0 0 0 10.2 5a4.7 4.7 0 0 0 3.7-1.8Z"
          transform="translate(1.2 1) scale(.9)"
        />
      </svg>
    </span>
  )
}

export function ProfileAvatar({ profile, token }) {
  const source = useAvatarSource(profile?.avatar_url, token)
  return source ? <img src={source} alt="" /> : initials(profile)
}

/* The identity "membership card": a tactile dark card with an iridescent edge.
   On pointer devices it tilts toward the cursor and its sheen follows; on
   touch devices it floats gently for a few seconds after it appears or is
   touched, then rests. Reduced motion disables both. */
// Each float frame repaints the masked conic-gradient sheen, so the float is
// a brief flourish rather than a continuous loop, and never runs while hidden.
const FLOAT_MS = 6000

function IdentityCard({ children, footer }) {
  const cardRef = useRef(null)
  const motionRef = useRef({ hover: false, reduced: false })
  const floatRef = useRef(null)

  useEffect(() => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const hover = window.matchMedia('(hover: hover)').matches
    motionRef.current = { hover, reduced }
    const card = cardRef.current
    if (!card || reduced || hover) return undefined
    let t = 0
    let timer = null
    let stopAt = 0
    const stop = () => {
      if (timer !== null) clearInterval(timer)
      timer = null
    }
    const float = () => {
      if (document.visibilityState === 'hidden') return
      stopAt = Date.now() + FLOAT_MS
      if (timer !== null) return
      timer = setInterval(() => {
        if (Date.now() >= stopAt || document.visibilityState === 'hidden') {
          stop()
          return
        }
        t += 0.02
        card.style.transform =
          `rotateX(${(Math.sin(t) * 2.6).toFixed(2)}deg) rotateY(${(Math.cos(t * 0.8) * 3.2).toFixed(2)}deg)`
        card.style.setProperty('--id-holo', `${(210 + Math.sin(t * 0.6) * 60).toFixed(0)}deg`)
      }, 50)
    }
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') stop()
      else float()
    }
    floatRef.current = float
    float()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      floatRef.current = null
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])

  const onMove = event => {
    const card = cardRef.current
    const { hover, reduced } = motionRef.current
    if (!card || !hover || reduced) return
    const rect = card.getBoundingClientRect()
    const px = (event.clientX - rect.left) / rect.width
    const py = (event.clientY - rect.top) / rect.height
    card.style.transform =
      `rotateX(${((0.5 - py) * 10).toFixed(2)}deg) rotateY(${((px - 0.5) * 12).toFixed(2)}deg)`
    card.style.setProperty('--id-mx', `${(px * 100).toFixed(1)}%`)
    card.style.setProperty('--id-my', `${(py * 100).toFixed(1)}%`)
    card.style.setProperty('--id-holo', `${(180 + px * 120).toFixed(0)}deg`)
  }

  const onLeave = () => {
    const card = cardRef.current
    if (card) card.style.transform = ''
  }

  return (
    <div className="id-tilt-zone" onPointerMove={onMove} onPointerLeave={onLeave} onPointerDown={() => floatRef.current?.()}>
      <div className="id-card-3d" ref={cardRef}>
        <div className="id-cardhead">
          <span className="id-cardword">Möbius · You</span>
          <span className="id-cardring" aria-hidden="true" />
        </div>
        {children}
        {footer}
      </div>
    </div>
  )
}

function HandleModal({ current, onClose, onSave, required = false }) {
  const [value, setValue] = useState(current || '')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef(null)
  const dialogRef = useDialog(onClose, pending || required, inputRef)
  const valid = /^[a-z0-9_]{3,30}$/.test(value)

  const save = async event => {
    event.preventDefault()
    if (!valid || pending) return
    setPending(true)
    setError('')
    try {
      await onSave(value)
      onClose()
    } catch (requestError) {
      setError(requestError.message)
    } finally {
      setPending(false)
    }
  }

  return (
    <div
      className="id-modal-backdrop"
      onMouseDown={event => {
        if (!pending && !required && event.target === event.currentTarget) onClose()
      }}
    >
      <form
        ref={dialogRef}
        className="id-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="handle-title"
        aria-describedby="handle-description handle-hint"
        aria-busy={pending}
        tabIndex={-1}
        onSubmit={save}
      >
        <div className="id-handle-preview" aria-hidden="true">
          @{value || 'you'}
        </div>
        <h2 id="handle-title">
          {required ? 'Choose your Möbius handle' : 'Change your handle'}
        </h2>
        <p id="handle-description">
          Your handle is your unique identity across Möbius. It is public when you choose to use it; your email stays private.
        </p>
        <label className="id-label" htmlFor="identity-handle">Handle</label>
        <div className="id-input-wrap">
          <span className="id-input-prefix" aria-hidden="true">@</span>
          <input
            ref={inputRef}
            id="identity-handle"
            className="id-input"
            value={value}
            maxLength={30}
            autoComplete="username"
            spellCheck="false"
            aria-invalid={Boolean(error) || !valid}
            aria-describedby="handle-hint"
            disabled={pending}
            onChange={event => {
              setValue(event.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ''))
              setError('')
            }}
          />
        </div>
        <div
          id="handle-hint"
          className={`id-hint${error ? ' is-error' : ''}`}
          role={error ? 'alert' : undefined}
        >
          {error || (valid
            ? 'Available handles are confirmed when you save.'
            : 'Use 3–30 letters, numbers or underscores.')}
        </div>
        <div className="id-modal-actions">
          {!required && (
            <button type="button" className="id-btn" disabled={pending} onClick={onClose}>
              Cancel
            </button>
          )}
          <button type="submit" className="id-btn id-btn--primary" disabled={!valid || pending}>
            {pending ? 'Claiming…' : required ? 'Claim handle' : 'Save handle'}
          </button>
        </div>
      </form>
    </div>
  )
}

function SignInModal({ token, onClose, onSignedIn }) {
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  const [completion, setCompletion] = useState(null)
  const popupRef = useRef(null)
  const waitAbortRef = useRef(null)
  const completionAbortRef = useRef(null)
  const completionBusyRef = useRef(false)
  const startBusyRef = useRef(false)
  const firstProviderRef = useRef(null)

  const cleanupWait = useCallback(() => {
    waitAbortRef.current?.abort()
    waitAbortRef.current = null
    try { popupRef.current?.close?.() } catch { /* popup is already cross-origin */ }
    popupRef.current = null
  }, [])

  const cancel = useCallback(() => {
    if (completionBusyRef.current) return
    cleanupWait()
    onClose()
  }, [cleanupWait, onClose])
  const dialogRef = useDialog(cancel, pending === 'complete', firstProviderRef)

  useEffect(() => () => {
    cleanupWait()
    completionAbortRef.current?.abort()
  }, [cleanupWait])

  const complete = async payload => {
    if (completionBusyRef.current) return
    completionBusyRef.current = true
    setPending('complete')
    setError('')
    const controller = new AbortController()
    completionAbortRef.current = controller
    try {
      const linked = await identityRequest(token, '/link/complete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: controller.signal,
      })
      setCompletion(null)
      onSignedIn(linked)
    } catch (requestError) {
      if (controller.signal.aborted) return

      // A lost response is ambiguous: the remote exchange and local commit may
      // already have succeeded. Read the authoritative state before offering a
      // retry so success never looks like failure and replay stays idempotent.
      let current = null
      try {
        current = await identityRequest(token)
      } catch { /* keep the original completion error */ }
      if (current?.account_mode === 'linked' && !current.account_unavailable) {
        setCompletion(null)
        onSignedIn(current)
        return
      }

      const retryable = requestError instanceof IdentityRequestError
        && (requestError.status === 0 || requestError.status >= 500)
      setCompletion(retryable ? payload : null)
      setError(retryable
        ? 'Your account approved the link, but this Möbius could not confirm completion. The approval is kept only in this dialog—retry completion.'
        : requestError.message)
    } finally {
      if (completionAbortRef.current === controller) completionAbortRef.current = null
      completionBusyRef.current = false
      setPending('')
    }
  }

  const begin = async provider => {
    if (pending || startBusyRef.current) return
    startBusyRef.current = true
    setPending(provider)
    setError('')
    setCompletion(null)
    const popup = window.open(
      'about:blank',
      'mobius-account-signin',
      'width=520,height=720',
    )
    if (!popup) {
      startBusyRef.current = false
      setPending('')
      setError(
        'Your browser blocked the sign-in window. Allow popups for this Möbius, then retry.',
      )
      return
    }

    popupRef.current = popup
    const controller = new AbortController()
    waitAbortRef.current = controller
    let completing = false
    try {
      const attempt = await identityRequest(token, '/link/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider }),
        signal: controller.signal,
      })
      const result = await waitForAccountLink({
        popup,
        attempt,
        signal: controller.signal,
      })
      waitAbortRef.current = null
      popupRef.current = null
      const payload = {
        code: result.code,
        state: result.state,
        attempt: attempt.attempt,
      }
      setCompletion(payload)
      completing = true
      await complete(payload)
    } catch (requestError) {
      if (!controller.signal.aborted) {
        setError(requestError.message || 'Sign-in could not start.')
      }
      cleanupWait()
    } finally {
      if (waitAbortRef.current === controller) waitAbortRef.current = null
      startBusyRef.current = false
      if (!completing) setPending('')
    }
  }

  const providerLabel = pending === 'google'
    ? 'Waiting for Google…'
    : pending === 'apple'
      ? 'Waiting for Apple…'
      : pending === 'complete'
        ? 'Finishing sign-in…'
        : ''

  return (
    <div
      className="id-modal-backdrop"
      onMouseDown={event => {
        if (pending !== 'complete' && event.target === event.currentTarget) cancel()
      }}
    >
      <section
        ref={dialogRef}
        className="id-modal id-signin-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="signin-title"
        aria-describedby="signin-description"
        aria-busy={pending === 'complete'}
        tabIndex={-1}
      >
        <div className="id-lock" aria-hidden="true">
          <Lock width={20} height={20} />
        </div>
        <h2 id="signin-title">Sign in to Möbius</h2>
        <p id="signin-description">
          Use the same account as mobius.you. You will return here automatically after consent.
        </p>
        <div className="id-provider-list">
          <button
            ref={firstProviderRef}
            type="button"
            className="id-provider"
            disabled={Boolean(pending)}
            onClick={() => begin('google')}
          >
            <GoogleMark />
            <span className="id-provider-copy">Continue with Google</span>
            <span className="id-provider-balance" aria-hidden="true" />
          </button>
          <button
            type="button"
            className="id-provider"
            disabled={Boolean(pending)}
            onClick={() => begin('apple')}
          >
            <AppleMark />
            <span className="id-provider-copy">Continue with Apple</span>
            <span className="id-provider-balance" aria-hidden="true" />
          </button>
        </div>
        <div className="id-progress" role="status" aria-live="polite">
          {providerLabel}
        </div>
        {error && <div className="id-signin-error" role="alert">{error}</div>}
        {completion && (
          <button
            type="button"
            className="id-btn id-btn--primary id-retry-completion"
            disabled={Boolean(pending)}
            onClick={() => complete(completion)}
          >
            Retry completion
          </button>
        )}
        <button
          type="button"
          className="id-btn id-cancel-signin"
          disabled={pending === 'complete'}
          onClick={cancel}
        >
          {pending === 'complete' ? 'Finishing…' : 'Cancel'}
        </button>
      </section>
    </div>
  )
}

function DisconnectModal({ token, onClose, onDisconnected, reconnecting = false }) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const keepRef = useRef(null)
  const dialogRef = useDialog(onClose, pending, keepRef)

  const disconnect = async () => {
    if (pending) return
    setPending(true)
    setError('')
    try {
      await identityRequest(token, '/link', { method: 'DELETE' })
      onDisconnected()
    } catch (requestError) {
      setError(requestError.status === 502
        ? 'Möbius could not confirm revocation, so your existing link was kept. Nothing was disconnected; try again later.'
        : requestError.message)
    } finally {
      setPending(false)
    }
  }

  return (
    <div
      className="id-modal-backdrop"
      onMouseDown={event => {
        if (!pending && event.target === event.currentTarget) onClose()
      }}
    >
      <section
        ref={dialogRef}
        className="id-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="disconnect-title"
        aria-describedby="disconnect-description"
        aria-busy={pending}
        tabIndex={-1}
      >
        <h2 id="disconnect-title">
          {reconnecting ? 'Reconnect this Möbius?' : 'Disconnect this Möbius?'}
        </h2>
        <p id="disconnect-description">
          {reconnecting
            ? 'Your account and deployments stay intact. The current link will be replaced so you can approve Railway management.'
            : 'Your mobius.you account and managed deployments stay intact. This self-hosted Möbius will stop showing or editing that account.'}
        </p>
        {error && <div className="id-signin-error" role="alert">{error}</div>}
        <div className="id-modal-actions">
          <button
            ref={keepRef}
            type="button"
            className="id-btn"
            disabled={pending}
            onClick={onClose}
          >
            Keep connected
          </button>
          <button
            type="button"
            className="id-btn id-btn--danger"
            disabled={pending}
            onClick={disconnect}
          >
            {pending
              ? reconnecting ? 'Preparing…' : 'Disconnecting…'
              : reconnecting ? 'Disconnect and reconnect' : 'Disconnect'}
          </button>
        </div>
      </section>
    </div>
  )
}

function Deployments({
  token,
  items,
  railway,
  selfHosted,
  onNew,
  onManage,
  managingDeployment,
  managingSection,
  onCloseManage,
  onCompute,
  onStorage,
  onRetry,
  onRename,
  onDelete,
  onConnect,
  onReconnect,
  onManageConnection,
  connecting,
}) {
  const managedById = new Map((railway?.instances || []).map(item => [item.id, item]))
  const deploymentOrigin = value => {
    try {
      const parsed = new URL(value)
      return parsed.protocol === 'https:' ? parsed.origin : ''
    } catch {
      return ''
    }
  }
  const managedByOrigin = new Map(
    (railway?.instances || [])
      .map(item => [deploymentOrigin(item.url), item])
      .filter(([origin]) => Boolean(origin)),
  )
  const deployments = [...items]
  for (const instance of railway?.instances || []) {
    if (!deployments.some(item => (
      item.id === instance.id
      || deploymentOrigin(item.url) === deploymentOrigin(instance.url)
    ))) {
      deployments.push({
        id: instance.id,
        name: instance.name,
        status: instance.status,
        url: instance.url,
        current: false,
      })
    }
  }
  const connected = railway?.railway_access === 'available'
    && railway.connection?.connected
  const tracking = (railway?.instances || []).some(deploymentNeedsTracking)

  return (
    <article className="id-card">
      <div className="id-card-head">
        <div>
          <h2>Your deployments</h2>
        </div>
        {tracking && (
          <div className="id-live-chip" role="status">
            <ArrowRotateCw className="id-spin" width={13} aria-hidden="true" />
            Updating live
          </div>
        )}
      </div>
      {railway?.railway_access === 'reconnect' && (
        <div className="id-railway-callout">
          <div>
            <strong>Approve Railway controls</strong>
            <span>Reconnect once to add the new deployment-management permission.</span>
          </div>
          <button type="button" className="id-btn" onClick={onReconnect}>Reconnect</button>
        </div>
      )}
      {railway?.railway_access === 'available' && !connected && (
        <div className="id-railway-callout">
          <div>
            <strong>{railway.connection ? 'Reconnect Railway' : 'Connect Railway'}</strong>
            <span>{railway.connection
              ? 'Railway authorization needs attention before this account can manage deployments.'
              : 'Connect your Railway account to create and manage Möbius deployments here.'}</span>
          </div>
          <button type="button" className="id-btn id-btn--primary" onClick={onConnect} disabled={connecting}>
            {connecting
              ? <><ArrowRotateCw className="id-spin" width={16} /> Connecting…</>
              : (railway.connection ? 'Reconnect Railway' : 'Connect Railway')}
          </button>
        </div>
      )}
      {railway?.railway_access === 'unavailable' && (
        <div className="id-railway-callout">
          <div>
            <strong>Railway controls are temporarily unavailable</strong>
            <span>Your confirmed deployment links remain below.</span>
          </div>
        </div>
      )}
      {connected && railway.connection?.deploy_blocked && (
        <div className="id-railway-callout id-railway-callout--warn">
          <div>
            <strong>Railway needs attention</strong>
            <span>{railway.connection.deploy_blocked}</span>
          </div>
        </div>
      )}
      <div className="id-deployments">
        {deployments.map(item => {
          const managed = managedById.get(item.id)
            || managedByOrigin.get(deploymentOrigin(item.url))
          const building = deploymentIsBuilding(managed)
          const displayName = managed?.name || item.name
          const state = deploymentPresentation(managed || item)
          const StateIcon = state.tone === 'success'
            ? CheckCircle
            : state.tone === 'danger'
              ? Warning
              : state.tone === 'progress'
                ? ArrowRotateCw
                : null
          return (
          <div className={`id-deployment id-deployment--${state.tone}`} key={item.id}>
            <div className="id-deployment-main">
              <div className="id-deploy-mark">
                <img src="/moebius.png" alt="" />
              </div>
              <div className="id-deploy-copy">
                <div className="id-deploy-name-row">
                  <div className="id-deploy-name">{displayName}</div>
                  {managed?.status === 'ready' && onRename && (
                    <DeploymentNameEditor
                      name={displayName}
                      disabled={deploymentNeedsTracking(managed)}
                      onSave={name => onRename(managed.id, { name })}
                    />
                  )}
                  {item.current && <span className="id-current-chip">You're here</span>}
                </div>
                {(item.region || (item.current && selfHosted && !managed)) && (
                  <div className="id-deploy-meta">
                    {item.region || ''}
                    {item.region && item.current && selfHosted && !managed ? ' · ' : ''}
                    {item.current && selfHosted && !managed ? 'Self-hosted' : ''}
                  </div>
                )}
                {state.detail && state.detail !== state.label && (
                  <div className={`id-deploy-detail id-deploy-detail--${state.tone}`}>
                    {state.detail}
                  </div>
                )}
              </div>
              <div className="id-deploy-actions">
                <span className={`id-status-pill id-status-pill--${state.tone}`}>
                  {StateIcon && (
                    <StateIcon
                      className={state.tone === 'progress' ? 'id-spin' : ''}
                      width={13}
                      aria-hidden="true"
                    />
                  )}
                  {state.label}
                </span>
                {!managed && item.url && !item.current && (
                  <button
                    type="button"
                    className="id-open"
                    aria-label={`Open ${displayName} in a new tab`}
                    onClick={() => window.open(item.url, '_blank', 'noopener,noreferrer')}
                  >
                    <ArrowUpRight width={18} />
                  </button>
                )}
              </div>
            </div>
            {managed && (
              // One labelled action row, matching the mobius.you dashboard:
              // compact enough to share a phone-width row, never icon-only.
              <div className={`id-deploy-buttons${building ? ' id-deploy-buttons--building' : ''}`}>
                {item.url && !item.current && !building && (
                  <button
                    type="button"
                    className="id-btn id-btn--primary"
                    aria-label={`Open ${displayName} in a new tab`}
                    onClick={() => window.open(item.url, '_blank', 'noopener,noreferrer')}
                  >
                    <ExternalLink width={14} aria-hidden="true" />
                    Open
                  </button>
                )}
                {managed.railway_url && (
                  <a
                    className="id-btn"
                    href={managed.railway_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={`Open ${displayName} project on Railway in a new tab`}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M6 15h12M6 19h12m-10 0-2 3m10-3 2 3M8 3h8M8 7h8"/><rect x="5" y="3" width="14" height="16" rx="2"/><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/></svg>
                    Railway
                  </a>
                )}
                {deploymentCanRecover(managed) && (
                  <button
                    type="button"
                    className="id-btn"
                    aria-label={`Recover ${displayName}`}
                    onClick={() => onManage(managed, 'recovery')}
                  >
                    <Lifesaver width={14} aria-hidden="true" />
                    Recover
                  </button>
                )}
                {managed.actions.delete && onDelete && (
                  <button
                    type="button"
                    className="id-btn id-btn--danger"
                    aria-label={building ? `Cancel deployment of ${displayName}` : `Delete ${displayName}`}
                    onClick={() => onDelete(managed)}
                  >
                    <Trash width={14} aria-hidden="true" />
                    {building ? 'Cancel deployment' : 'Delete'}
                  </button>
                )}
              </div>
            )}
            {managed?.status === 'ready' && (
              <UsageDisclosure token={token} instance={managed} />
            )}
            {managed && (managed.actions.edit_resources
              || (managed.actions.retry && !building)
              || (managingDeployment?.id === managed.id && managingSection === 'recovery')) && (
              <ManageDeploymentPanel
                key={`${managed.id}:${managingDeployment?.id === managed.id ? managingSection : 'resources'}:${managed.resources.cpu}:${managed.resources.memory_mb}:${managed.resources.volume_size_mb}`}
                instance={managed}
                section={managingDeployment?.id === managed.id ? managingSection : null}
                token={token}
                planLimits={railway?.connection?.plan_limits}
                onClose={onCloseManage}
                onCompute={onCompute}
                onStorage={onStorage}
                onRetry={onRetry}
              />
            )}
          </div>
          )
        })}
      </div>
      {connected && (
        <button type="button" className="id-add-row" onClick={onNew}>
          <span className="id-add-plus" aria-hidden="true"><Plus width={17} /></span>
          New deployment
        </button>
      )}
      {connected && railway.connection && (
        <div className="id-dep-foot">
          <span className="id-railway-conn-account">
            Railway workspace connected · {railway.connection.workspace || railway.connection.account || 'Connected'}
          </span>
          {planTitle(railway.connection.plan) && (
            <span className="id-railway-plan">{planTitle(railway.connection.plan)}</span>
          )}
          <a className="id-railway-plan-link" href="https://railway.com/workspace/plans" target="_blank" rel="noopener noreferrer">
            Manage plan on Railway <ArrowUpRight width={13} aria-hidden="true" />
          </a>
          {onManageConnection && (
            <button type="button" className="id-railway-manage" aria-label="Manage Railway account" onClick={onManageConnection}>
              Account
              <ChevronRight width={13} aria-hidden="true" />
            </button>
          )}
        </div>
      )}
    </article>
  )
}

function DeploymentNameEditor({ name, disabled, onSave }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(name)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef(null)

  useEffect(() => {
    if (!editing) setValue(name)
  }, [editing, name])

  useEffect(() => {
    if (editing) inputRef.current?.focus()
  }, [editing])

  const close = () => {
    if (pending) return
    setEditing(false)
    setError('')
    setValue(name)
  }

  const submit = async event => {
    event.preventDefault()
    const next = value.trim()
    if (!next || next === name || pending) return
    setPending(true)
    setError('')
    try {
      await onSave(next)
      setEditing(false)
    } catch (requestError) {
      setError(requestError.message)
    } finally {
      setPending(false)
    }
  }

  return (
    <div className={`id-name-editor${editing ? ' is-editing' : ''}`}>
      <button
        type="button"
        className="id-name-edit"
        aria-label={`Rename ${name}`}
        aria-expanded={editing}
        disabled={disabled}
        onClick={() => setEditing(current => !current)}
      >
        <Pencil width={14} />
      </button>
      {editing && (
        <form className="id-name-form" onSubmit={submit}>
          <input
            ref={inputRef}
            className="id-input id-input--boxed"
            value={value}
            maxLength={80}
            autoComplete="off"
            spellCheck="false"
            aria-label="Deployment name"
            disabled={pending}
            onChange={event => setValue(event.target.value)}
          />
          <div className="id-name-actions">
            <button type="button" className="id-btn id-btn--quiet" disabled={pending} onClick={close}>Cancel</button>
            <button type="submit" className="id-btn" disabled={pending || !value.trim() || value.trim() === name}>
              {pending ? 'Saving…' : 'Save'}
            </button>
          </div>
          {error && <div className="id-signin-error" role="alert">{error}</div>}
        </form>
      )}
    </div>
  )
}

function fmtCpu(n) {
  return `${n} vCPU`
}

function fmtMemory(mb) {
  return mb % 1024 === 0 ? `${mb / 1024} GB` : `${mb} MB`
}

function fmtVolume(mb) {
  return mb < 1000 ? `${mb} MB` : `${mb / 1000} GB`
}

function planTitle(label) {
  if (!label || label === 'unknown') return ''
  return label.charAt(0).toUpperCase() + label.slice(1)
}

// Plan-bounded CPU / RAM / (optional) storage pickers, mirroring the resource
// choices the mobius.you website offers. `limits` is connection.plan_limits from
// the account host; when it is absent the component renders nothing so the modal
// gracefully falls back to plan defaults. Storage is rendered only when onVolume
// is supplied; storageMinMb enforces Railway's grow-only rule in the manage flow.
function ResourceFields({
  limits, cpu, memory, volume, onCpu, onMemory, onVolume, disabled, storageMinMb = 0,
}) {
  if (!limits) return null
  // Keep whatever the deployment currently sits on selectable even if it is not
  // one of the plan's listed steps, so the control never silently snaps to
  // "Plan maximum" and submit a value the user did not choose.
  const withCurrent = (choices, raw) => {
    const value = Number(String(raw ?? '').trim())
    return raw && Number.isFinite(value) && !choices.includes(value)
      ? [...choices, value].sort((a, b) => a - b)
      : choices
  }
  const cpuChoices = withCurrent(limits.cpu_choices.filter(value => value < limits.max_cpu), cpu)
  const memChoices = withCurrent(limits.memory_options_mb.filter(value => value < limits.max_memory_mb), memory)
  const volChoices = withCurrent(limits.volume_options_mb.filter(value => value >= storageMinMb), volume)
  return (
    <div className="id-resource-fields">
      <label className="id-field-block">
        <span className="id-label">CPU</span>
        <select className="id-select" value={cpu} disabled={disabled} onChange={event => onCpu(event.target.value)}>
          <option value="">Plan maximum · {fmtCpu(limits.max_cpu)}</option>
          {cpuChoices.map(value => <option key={value} value={value}>{fmtCpu(value)}</option>)}
        </select>
      </label>
      <label className="id-field-block">
        <span className="id-label">RAM</span>
        <select className="id-select" value={memory} disabled={disabled} onChange={event => onMemory(event.target.value)}>
          <option value="">Plan maximum · {fmtMemory(limits.max_memory_mb)}</option>
          {memChoices.map(value => <option key={value} value={value}>{fmtMemory(value)}</option>)}
        </select>
      </label>
      {onVolume && (
        <label className="id-field-block">
          <span className="id-label">Storage</span>
          <select className="id-select" value={volume} disabled={disabled} onChange={event => onVolume(event.target.value)}>
            {volChoices.map(value => <option key={value} value={value}>{fmtVolume(value)}</option>)}
          </select>
          {storageMinMb > 0 && <small>Railway volumes can only grow.</small>}
        </label>
      )}
    </div>
  )
}

function fmtUsd(value) {
  if (typeof value !== 'number') return ''
  return `$${Number.isInteger(value) ? value : value.toFixed(2)}`
}

function WandIcon(props) {
  return (
    <svg viewBox="0 0 24 24" width={props.width || 16} height={props.width || 16} fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4.5 16.5c-1.5 1.3-2 5-2 5s3.7-.5 5-2c.7-.8.7-2.1-.1-2.9a2 2 0 0 0-2.9-.1ZM12 15l-3-3a22 22 0 0 1 8-11 11 11 0 0 1 6 6 22 22 0 0 1-11 8Z" />
    </svg>
  )
}

// Mirrors the mobius.you deploy composer: name + a live "included / storage"
// launch summary, resources, and access tucked behind Advanced settings, plus
// a Deploy Möbius action. Container updates stay in the normal Settings flow.
function NewDeploymentModal({
  onClose, onCreate, planLimits, plan, regionOptions,
}) {
  const [name, setName] = useState('My Möbius')
  const [managedAuth, setManagedAuth] = useState(true)
  const [cpu, setCpu] = useState(() => planLimits
    ? String(planLimits.default_cpu ?? Math.min(2, planLimits.max_cpu)) : '')
  const [memory, setMemory] = useState(() => planLimits
    ? String(planLimits.default_memory_mb ?? Math.min(4096, planLimits.max_memory_mb)) : '')
  const [volume, setVolume] = useState(planLimits ? String(planLimits.default_volume_mb) : '')
  const [region, setRegion] = useState(() => {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ''
    const suggestion = suggestRailwayRegion(zone, -new Date().getTimezoneOffset())
    return regionOptions?.some(option => option.id === suggestion) ? suggestion : ''
  })
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef(null)
  const dialogRef = useDialog(onClose, pending, inputRef)

  const submit = async event => {
    event.preventDefault()
    if (!name.trim() || pending) return
    setPending(true)
    setError('')
    try {
      const settings = {
        name: name.trim(),
        managed_auth: managedAuth,
        cpu: cpu ? Number(cpu) : null,
        memory_mb: memory ? Number(memory) : null,
        volume_mb: volume ? Number(volume) : null,
      }
      if (regionOptions?.length && region) settings.region = region
      await onCreate(settings)
      onClose()
    } catch (requestError) {
      setError(requestError.message)
    } finally {
      setPending(false)
    }
  }

  const planName = planTitle(plan)
  const included = planLimits && typeof planLimits.included_usd === 'number'
    ? fmtUsd(planLimits.included_usd)
    : ''
  const storageLabel = planLimits
    ? fmtVolume(Number(volume) || planLimits.default_volume_mb)
    : ''
  const regionLabel = regionOptions?.length
    ? (regionOptions.find(option => option.id === region)?.label || 'Railway preferred region')
    : ''
  const advancedSummary = [
    storageLabel || (planLimits ? 'Default resources' : ''),
    regionLabel,
    managedAuth ? 'Möbius sign-in on' : 'Local sign-in',
  ].filter(Boolean).join(' · ')
  const summary = []
  if (included) {
    summary.push(<span key="inc"><b>{included}</b> included{planName ? ` on ${planName}` : ''}</span>)
  }
  if (storageLabel) {
    summary.push(<span key="sto"><b>{storageLabel}</b> persistent storage</span>)
  }
  const summaryRow = []
  summary.forEach((item, index) => {
    if (index > 0) summaryRow.push(<i key={`sep${index}`} aria-hidden="true" />)
    summaryRow.push(item)
  })

  return (
    <div className="id-modal-backdrop" onMouseDown={event => {
      if (!pending && event.target === event.currentTarget) onClose()
    }}>
      <form
        ref={dialogRef}
        className="id-modal id-composer-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-deployment-title"
        aria-busy={pending}
        tabIndex={-1}
        onSubmit={submit}
      >
        <h2 id="new-deployment-title">Name your Möbius</h2>
        <p className="id-composer-sub">We’ll build it in your Railway account.</p>

        <label className="id-label" htmlFor="deployment-name">Name</label>
        <div className="id-input-wrap id-input-wrap--tick">
          <input
            ref={inputRef}
            id="deployment-name"
            className="id-input"
            value={name}
            maxLength={80}
            autoComplete="off"
            spellCheck="false"
            disabled={pending}
            onChange={event => setName(event.target.value)}
          />
          {name.trim() && (
            <span className="id-tick" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round"><path d="m5 13 4 4L19 7" /></svg>
            </span>
          )}
        </div>

        {summaryRow.length > 0 && (
          <p className="id-launch-summary">{summaryRow}</p>
        )}

        {(planLimits || regionOptions?.length > 0) ? (
          <details className="id-disclosure">
            <summary>
              <span className="id-disclosure-title">Advanced settings</span>
              <span className="id-disclosure-state">
                {advancedSummary}
              </span>
              <span className="id-disclosure-caret" aria-hidden="true">
                <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 6 6 6-6 6" /></svg>
              </span>
            </summary>
            <div className="id-disclosure-body">
              {planLimits && (
                <>
                  <p className="id-eyebrow">Resources</p>
                  <ResourceFields
                    limits={planLimits}
                    cpu={cpu}
                    memory={memory}
                    volume={volume}
                    onCpu={setCpu}
                    onMemory={setMemory}
                    onVolume={setVolume}
                    disabled={pending}
                  />
                </>
              )}
              {regionOptions?.length > 0 && (
                <div className="id-region-group">
                  <label className="id-field-block">
                    <span className="id-label">Deployment region</span>
                    <select className="id-select" value={region} disabled={pending} onChange={event => setRegion(event.target.value)}>
                      <option value="">Railway preferred region</option>
                      {regionOptions.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
                    </select>
                    <small>We suggest a region when your browser’s time zone allows it. Check before launch; moving storage later can interrupt service.</small>
                  </label>
                </div>
              )}
              <p className="id-eyebrow">Access</p>
              <label className="id-switch">
                <input
                  type="checkbox"
                  className="id-switch-input"
                  checked={managedAuth}
                  disabled={pending}
                  onChange={event => setManagedAuth(event.target.checked)}
                />
                <span className="id-switch-track" aria-hidden="true" />
                <span className="id-switch-copy">
                  <strong>Sign in with Möbius</strong>
                  <span>Secure your Möbius with your mobius.you account. Disable this to set up a custom username and password on first boot.</span>
                </span>
              </label>
              <p className="id-cost-note">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 11v5" /><path d="M12 7.5h.01" /></svg>
                <span><strong>Möbius doesn't charge you.</strong> We use Railway to make launching your Möbius agents as seamless as possible; Railway bills your own account for actual usage.</span>
              </p>
            </div>
          </details>
        ) : (
          <label className="id-check-row">
            <input
              type="checkbox"
              checked={managedAuth}
              disabled={pending}
              onChange={event => setManagedAuth(event.target.checked)}
            />
            <span>
              <strong>Sign in with Möbius</strong>
              <small>Secure it with your mobius.you account. Disable this to set up a custom username and password on first boot.</small>
            </span>
          </label>
        )}

        {error && <div className="id-signin-error" role="alert">{error}</div>}

        <div className="id-composer-foot">
          <p className="id-composer-note">
            Möbius follows each build step until your private link is ready. You can close this and come back anytime.
          </p>
          <div className="id-modal-actions">
            <button type="button" className="id-btn" disabled={pending} onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="id-btn id-btn--primary id-deploy-btn" disabled={!name.trim() || pending}>
              {pending ? 'Deploying…' : <><WandIcon width={16} /> Deploy Möbius</>}
            </button>
          </div>
        </div>
      </form>
    </div>
  )
}

function meterPercent(value) {
  const parsed = parseFloat(String(value ?? '').replace('%', ''))
  return Number.isFinite(parsed) ? Math.max(0, Math.min(100, parsed)) : 0
}

function formatDeployedAt(iso) {
  if (!iso) return ''
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) return ''
  const mins = Math.round((Date.now() - then.getTime()) / 60000)
  if (mins < 1) return 'Deployed just now'
  if (mins < 60) return `Deployed ${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `Deployed ${hrs}h ago`
  const days = Math.round(hrs / 24)
  if (days < 30) return `Deployed ${days}d ago`
  return `Deployed ${then.toLocaleDateString()}`
}

function MetricMeter({ label, value, limit, percent }) {
  const pct = meterPercent(percent)
  return (
    <div className="id-meter">
      <div className="id-meter-head">
        <span className="id-meter-label">{label}</span>
        <span className="id-meter-value">
          {value || '—'}{limit ? <span className="id-meter-limit"> / {limit}</span> : null}
        </span>
      </div>
      <div className="id-meter-track">
        <div className="id-meter-fill" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

// Live usage polls Railway every 15 seconds, so it stays collapsed and only
// mounts (and fetches) while the owner has it open.
function UsageDisclosure({ token, instance }) {
  const [open, setOpen] = useState(false)
  // Same panel frame as Resources so both rows share spacing, divider and inset.
  return (
    <section className="id-manage-panel" aria-label="Usage">
      <div className="id-manage-settings">
        <details className="id-disclosure id-manage-disclosure" onToggle={event => setOpen(event.currentTarget.open)}>
          <summary>
            <span className="id-disclosure-title">Usage</span>
            <span className="id-disclosure-state">CPU, RAM, storage and network</span>
            <span className="id-disclosure-caret" aria-hidden="true">
              <ChevronRight width={15} />
            </span>
          </summary>
          {open && (
            <div className="id-disclosure-body">
              <DeploymentMetrics token={token} instance={instance} compact />
            </div>
          )}
        </details>
      </div>
    </section>
  )
}

function DeploymentMetrics({ token, instance, compact = false }) {
  const [metrics, setMetrics] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (instance.status !== 'ready') return undefined
    let controller
    let timer
    let cancelled = false
    setMetrics(null)
    setError('')
    const refresh = async () => {
      if (document.visibilityState !== 'visible') return
      controller?.abort()
      const requestController = new AbortController()
      controller = requestController
      try {
        const data = await identityRequest(
          token, `/railway/deployments/${instance.id}/metrics`, { signal: requestController.signal },
        )
        if (!requestController.signal.aborted && !cancelled) {
          setMetrics(data)
          setError('')
        }
      } catch (requestError) {
        if (!requestController.signal.aborted && !cancelled) setError(requestError.message)
      } finally {
        // Focus and visibility can both resume polling before the earlier
        // request unwinds. Only the newest request owns the next pulse;
        // otherwise every overlap leaves behind another repeating timer.
        if (!cancelled && controller === requestController) {
          timer = setTimeout(refresh, 15000)
        }
      }
    }
    const resume = () => {
      if (document.visibilityState !== 'visible') return
      clearTimeout(timer)
      void refresh()
    }
    void refresh()
    window.addEventListener('focus', resume)
    document.addEventListener('visibilitychange', resume)
    return () => {
      cancelled = true
      clearTimeout(timer)
      controller?.abort()
      window.removeEventListener('focus', resume)
      document.removeEventListener('visibilitychange', resume)
    }
  }, [instance.id, instance.status, token])

  if (instance.status !== 'ready') return null
  if (error && !metrics) return <div className="id-metrics-note">Live metrics are unavailable right now.</div>
  // Render the meter structure immediately so the manage view opens complete;
  // values fill in when the fetch returns rather than gating on a spinner.
  const runtime = metrics?.runtime || {}
  const runtimeBits = [
    runtime.status_label,
    runtime.region_label,
    formatDeployedAt(runtime.latest_deployment_at),
    runtime.data_status,
  ].filter(Boolean)
  return (
    <div className={`id-metrics${compact ? ' id-metrics--card' : ''}${metrics ? '' : ' is-loading'}`}>
      {metrics
        ? runtimeBits.length > 0 && <div className="id-metrics-runtime">{runtimeBits.join(' · ')}</div>
        : (
          <div className="id-metrics-runtime" role="status">
            <ArrowRotateCw className="id-spin" width={13} aria-hidden="true" /> Loading live metrics…
          </div>
        )}
      <div className="id-meters">
        <MetricMeter label="CPU" value={metrics?.cpu?.label} limit={metrics?.cpu?.limit_label} percent={metrics?.cpu?.percent} />
        <MetricMeter label="RAM" value={metrics?.memory?.label} limit={metrics?.memory?.limit_label} percent={metrics?.memory?.percent} />
        <MetricMeter label="Storage" value={metrics?.volume?.used_label} limit={metrics?.volume?.allocated_label} percent={metrics?.volume?.percent} />
        <MetricMeter
          label="Network"
          value={metrics?.network?.rx_label || metrics?.network?.tx_label
            ? `↓ ${metrics?.network?.rx_label || '0'} · ↑ ${metrics?.network?.tx_label || '0'}`
            : ''}
          percent={metrics?.network?.percent}
        />
      </div>
    </div>
  )
}

function RecoverySection({ token, instance }) {
  // "Open Recovery" starts an ephemeral Railway worker on the account host and,
  // when it reports ready, hands off to the worker's own web UI in a popup — the
  // same web flow the mobius.you website uses. No credentials touch this frame.
  const [recovery, setRecovery] = useState(null)
  const pollRef = useRef(null)
  const busyRef = useRef(false)

  useEffect(() => () => {
    clearTimeout(pollRef.current)
    busyRef.current = false
  }, [])

  const poll = useCallback(async () => {
    try {
      const status = await identityRequest(token, `/railway/deployments/${instance.id}/recovery/status`)
      setRecovery({ state: status.state, message: status.message, error: status.error })
      if (status.state === 'ready' && status.open_url) {
        window.open(status.open_url, 'mobius-recovery', 'width=940,height=760')
        busyRef.current = false
        return
      }
      if (status.state === 'starting') {
        pollRef.current = setTimeout(poll, 2500)
      } else {
        busyRef.current = false
      }
    } catch (requestError) {
      busyRef.current = false
      setRecovery({ state: 'error', message: requestError.message, error: '' })
    }
  }, [token, instance.id])

  const start = async () => {
    if (busyRef.current) return
    busyRef.current = true
    setRecovery({ state: 'starting', message: 'Starting a temporary recovery worker\u2026', error: '' })
    try {
      await identityRequest(token, `/railway/deployments/${instance.id}/recovery`, { method: 'POST' })
      poll()
    } catch (requestError) {
      busyRef.current = false
      setRecovery({ state: 'error', message: requestError.message, error: '' })
    }
  }

  const preparing = recovery?.state === 'starting'
  return (
    <div className="id-recovery">
      <button type="button" className="id-btn id-recovery-btn" disabled={preparing} onClick={start}>
        {preparing
          ? <><ArrowRotateCw className="id-spin" width={16} /> Preparing Recovery…</>
          : 'Open Recovery'}
      </button>
      {recovery && recovery.state !== 'starting' && (
        <div
          className={`id-recovery-note${recovery.state === 'error' ? ' is-error' : ''}`}
          role="status"
        >
          {recovery.state === 'ready'
            ? 'Recovery opened in a new window.'
            : (recovery.error || recovery.message)}
        </div>
      )}
      {!recovery && (
        <small className="id-recovery-hint">
          Opens a temporary, isolated worker to inspect and repair this deployment.
        </small>
      )}
    </div>
  )
}

function DeletionRecoverySection({
  token, instance, pending, onConfirmAbsent,
}) {
  const [diagnosis, setDiagnosis] = useState(null)
  const [checkError, setCheckError] = useState('')
  const [unsupported, setUnsupported] = useState(false)
  const [confirmRecord, setConfirmRecord] = useState(false)
  const [revision, setRevision] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setDiagnosis(null)
    setCheckError('')
    setUnsupported(false)
    ;(async () => {
      try {
        const result = await identityRequest(
          token,
          `/railway/deployments/${instance.id}/deletion`,
          { signal: controller.signal },
        )
        if (!controller.signal.aborted) setDiagnosis(result)
      } catch (requestError) {
        if (controller.signal.aborted) return
        // Older Möbius hosts do not have this read-only check yet. Preserve the
        // existing retry path rather than turning a staged upgrade into an error.
        if (requestError.status === 404) setUnsupported(true)
        else setCheckError(requestError.message)
      }
    })()
    return () => controller.abort()
  }, [instance.id, revision, token])

  const checking = !diagnosis && !checkError && !unsupported
  const title = checking
    ? 'Checking the Railway project'
    : diagnosis?.state === 'present'
      ? 'Railway still shows this project'
      : ['missing', 'missing_unconfirmed'].includes(diagnosis?.state)
        ? 'Deletion may already be complete'
        : diagnosis?.state === 'authorization'
          ? 'Reconnect Railway to continue'
          : 'Railway could not confirm deletion'
  const message = checking
    ? 'This read-only check does not change your deployment.'
    : diagnosis?.message
      || checkError
      || 'Try deleting again, or open Railway to check the project directly.'
  const RecoveryIcon = checking ? ArrowRotateCw : diagnosis?.can_confirm_absent ? CheckCircle : Warning
  const absenceNeedsOwnerCheck = diagnosis?.state === 'missing_unconfirmed'

  return (
    <section className="id-deletion-recovery" aria-labelledby="deletion-recovery-title">
      <div className="id-deletion-recovery-head">
        <RecoveryIcon
          className={checking ? 'id-spin' : ''}
          width={18}
          aria-hidden="true"
        />
        <div>
          <h3 id="deletion-recovery-title">{title}</h3>
          <p>{message}</p>
        </div>
      </div>

      <div className="id-deletion-recovery-actions">
        {instance.railway_url && (
          <button
            type="button"
            className="id-btn"
            disabled={Boolean(pending)}
            onClick={() => window.open(instance.railway_url, '_blank', 'noopener,noreferrer')}
          >
            Open Railway <ArrowUpRight width={16} />
          </button>
        )}
        {checkError && (
          <button
            type="button"
            className="id-btn"
            disabled={Boolean(pending)}
            onClick={() => setRevision(value => value + 1)}
          >
            Check again
          </button>
        )}
        {diagnosis?.can_confirm_absent && !confirmRecord && (
          <button
            type="button"
            className="id-btn id-btn--quiet"
            disabled={Boolean(pending)}
            onClick={() => setConfirmRecord(true)}
          >
            Remove from this list
          </button>
        )}
      </div>

      {confirmRecord && (
        <div className="id-absence-confirm">
          <strong>{absenceNeedsOwnerCheck
            ? 'Did you check that the project is gone in Railway?'
            : 'Remove this finished deployment from the list?'}</strong>
          <span>{absenceNeedsOwnerCheck
            ? 'This only removes the finished record from Möbius. It does not delete anything in Railway.'
            : 'Möbius will check Railway once more, then remove only the local record.'}</span>
          <div>
            <button
              type="button"
              className="id-btn"
              disabled={Boolean(pending)}
              onClick={() => setConfirmRecord(false)}
            >
              Not yet
            </button>
            <button
              type="button"
              className="id-btn id-btn--danger"
              disabled={Boolean(pending)}
              onClick={onConfirmAbsent}
            >
              {pending === 'confirm-absent'
                ? 'Removing…'
                : absenceNeedsOwnerCheck ? 'I checked — remove it' : 'Remove finished deployment'}
            </button>
          </div>
        </div>
      )}
    </section>
  )
}

function ManageDeploymentPanel({
  instance, section, onClose, onCompute, onStorage, onRetry, planLimits, token,
}) {
  // Selects use '' to mean "plan maximum"; if the deployment already sits at the
  // plan ceiling, start there rather than on a value the picker would not list.
  const [cpu, setCpu] = useState(() => {
    const current = instance.resources.cpu ? String(instance.resources.cpu) : ''
    return planLimits && Number(current) >= planLimits.max_cpu ? '' : current
  })
  const [memory, setMemory] = useState(() => {
    const current = instance.resources.memory_mb ? String(instance.resources.memory_mb) : ''
    return planLimits && Number(current) >= planLimits.max_memory_mb ? '' : current
  })
  const [volume, setVolume] = useState(() => {
    const current = instance.resources.volume_size_mb
    if (!current) return ''
    return String(planLimits?.volume_options_mb.find(value => value > current) ?? current)
  })
  const [confirmVolume, setConfirmVolume] = useState(null)
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  const resourceSummary = instance.resources.volume_size_mb
    ? 'Change CPU or RAM, or increase storage'
    : 'Change CPU or RAM'
  const canRetryDeployment = instance.status !== 'delete_failed' && instance.actions.retry

  const run = async (action, work) => {
    if (pending) return
    setPending(action)
    setError('')
    try {
      await work()
      onClose()
    } catch (requestError) {
      setError(requestError.message)
    } finally {
      setPending('')
    }
  }

  return (
      <section
        id={`manage-${instance.id}`}
        className="id-manage-panel"
        aria-label={`Deployment options for ${instance.name}`}
        aria-busy={Boolean(pending)}
      >
        {canRetryDeployment && (
          <div className="id-manage-retry">
            <div>
              <strong>Deployment needs attention</strong>
              <span>{instance.last_error || 'Railway could not finish this deployment.'}</span>
            </div>
            <button
              type="button"
              className="id-btn id-btn--primary"
              disabled={Boolean(pending)}
              onClick={() => run('retry', () => onRetry(instance.id))}
            >
              {pending === 'retry' ? 'Retrying…' : 'Try deployment again'}
            </button>
          </div>
        )}

        <div className="id-manage-settings">
          {instance.actions.edit_resources && (
            <details className="id-disclosure id-manage-disclosure" open={section === 'resources' || undefined}>
              <summary>
                <span className="id-disclosure-title">Resources</span>
                <span className="id-disclosure-state">{resourceSummary}</span>
                <span className="id-disclosure-caret" aria-hidden="true">
                  <ChevronRight width={15} />
                </span>
              </summary>
              <div className="id-disclosure-body id-manage-resources">
            {planLimits ? (
              <ResourceFields
                limits={planLimits}
                cpu={cpu}
                memory={memory}
                onCpu={setCpu}
                onMemory={setMemory}
                disabled={Boolean(pending)}
              />
            ) : (
              <div className="id-resource-fields">
                <label className="id-field-block">
                  <span className="id-label">CPU</span>
                  <input
                    className="id-input id-input--boxed"
                    inputMode="numeric"
                    value={cpu}
                    placeholder="Plan maximum"
                    disabled={Boolean(pending)}
                    onChange={event => setCpu(event.target.value.replace(/\D/g, ''))}
                  />
                </label>
                <label className="id-field-block">
                  <span className="id-label">RAM (MB)</span>
                  <input
                    className="id-input id-input--boxed"
                    inputMode="numeric"
                    value={memory}
                    placeholder="Plan maximum"
                    disabled={Boolean(pending)}
                    onChange={event => setMemory(event.target.value.replace(/\D/g, ''))}
                  />
                </label>
              </div>
            )}
            <button
              type="button"
              className="id-btn"
              disabled={Boolean(pending)}
              onClick={() => run('compute', () => onCompute(instance.id, {
                cpu: cpu ? Number(cpu) : null,
                memory_mb: memory ? Number(memory) : null,
              }))}
            >
              {pending === 'compute' ? 'Updating…' : 'Update compute'}
            </button>
            {instance.resources.volume_size_mb ? (
              <div className="id-manage-storage">
                <div className="id-manage-storage-intro">
                  <strong>Storage</strong>
                  <span>Currently {fmtVolume(instance.resources.volume_size_mb)}. Existing data stays in place when you grow it.</span>
                </div>
                <div className="id-storage-row">
                  <label className="id-field-block">
                    <span className="id-label">Increase to</span>
                    {planLimits ? (
                      <select
                        className="id-select"
                        value={volume}
                        disabled={Boolean(pending) || confirmVolume !== null}
                        onChange={event => { setVolume(event.target.value); setConfirmVolume(null) }}
                      >
                        {planLimits.volume_options_mb
                          .filter(value => value >= instance.resources.volume_size_mb)
                          .map(value => <option key={value} value={value}>{fmtVolume(value)}</option>)}
                      </select>
                    ) : (
                      <input
                        className="id-input id-input--boxed"
                        inputMode="numeric"
                        value={volume}
                        disabled={Boolean(pending) || confirmVolume !== null}
                        onChange={event => { setVolume(event.target.value.replace(/\D/g, '')); setConfirmVolume(null) }}
                      />
                    )}
                  </label>
                  {confirmVolume === null && <button
                    type="button"
                    className="id-btn"
                    disabled={Boolean(pending) || !volume || Number(volume) <= instance.resources.volume_size_mb}
                    onClick={() => setConfirmVolume(Number(volume))}
                  >
                    Review increase
                  </button>}
                </div>
                {confirmVolume !== null && (
                  <div className="id-storage-confirm" role="group" aria-label="Confirm storage increase">
                    <strong>Increase storage to {fmtVolume(confirmVolume)}?</strong>
                    <p>This volume can only grow. You won’t be able to reduce it later.</p>
                    <div className="id-storage-confirm-actions">
                      <button type="button" className="id-btn" disabled={Boolean(pending)} onClick={() => setConfirmVolume(null)}>Keep current size</button>
                      <button
                        type="button"
                        className="id-btn id-btn--primary"
                        disabled={Boolean(pending)}
                        onClick={() => run('storage', () => onStorage(instance.id, { volume_mb: confirmVolume }))}
                      >
                        {pending === 'storage' ? 'Increasing…' : `Increase to ${fmtVolume(confirmVolume)}`}
                      </button>
                    </div>
                  </div>
                )}
                <p className="id-storage-limit-note">Attached storage can only be increased, not reduced.</p>
              </div>
            ) : null}
              </div>
            </details>
          )}

          {section === 'recovery' && deploymentCanRecover(instance) && (
            <details className="id-disclosure id-manage-disclosure" open={section === 'recovery' || undefined}>
              <summary>
                <span className="id-disclosure-title">Recovery</span>
                <span className="id-disclosure-state">Open a temporary repair session</span>
                <span className="id-disclosure-caret" aria-hidden="true">
                  <ChevronRight width={15} />
                </span>
              </summary>
              <div className="id-disclosure-body id-manage-recovery">
                <RecoverySection token={token} instance={instance} />
              </div>
            </details>
          )}
        </div>

        {error && <div className="id-signin-error" role="alert">{error}</div>}

      </section>
  )
}

function DeleteDeploymentModal({
  instance, token, onClose, onRetry, onDelete, onConfirmAbsent,
}) {
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const closeRef = useRef(null)
  const dialogRef = useDialog(onClose, Boolean(pending), closeRef)
  const retryingDelete = String(instance.status).toLowerCase() === 'delete_failed'
  const cancellingBuild = deploymentIsBuilding(instance)

  const run = async (action, work) => {
    if (pending) return
    setPending(action)
    setError('')
    try {
      await work()
      onClose()
    } catch (requestError) {
      setError(requestError.message)
    } finally {
      setPending('')
    }
  }

  return (
    <div className="id-modal-backdrop" onMouseDown={event => {
      if (!pending && event.target === event.currentTarget) onClose()
    }}>
      <section
        ref={dialogRef}
        className="id-modal id-manage-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-deployment-title"
        aria-busy={Boolean(pending)}
        tabIndex={-1}
      >
        <div className="id-manage-head">
          <div>
            <h2 id="delete-deployment-title">{cancellingBuild ? `Cancel ${instance.name}` : `Delete ${instance.name}`}</h2>
            <p>{cancellingBuild ? 'Stop this deployment build' : 'Permanent deployment removal'}</p>
          </div>
        </div>

        {retryingDelete && (
          <DeletionRecoverySection
            token={token}
            instance={instance}
            pending={pending}
            onConfirmAbsent={() => run(
              'confirm-absent',
              () => onConfirmAbsent(instance.id),
            )}
          />
        )}

        {error && <div className="id-signin-error" role="alert">{error}</div>}

        {confirmDelete ? (
          <div className="id-delete-confirm">
            <strong>{retryingDelete
              ? 'Try removing this Railway project again?'
              : cancellingBuild
                ? 'Cancel this deployment?'
                : 'Delete this Möbius and its Railway project?'}</strong>
            <span>{retryingDelete
              ? 'Möbius will ask Railway to permanently delete it again, then keep this page updated.'
              : cancellingBuild
                ? 'Setup will stop. If Railway has created a project and storage volume, they will be removed. This cannot be undone.'
                : 'This permanently removes the deployment and cannot be undone.'}</span>
            <div>
              <button type="button" className="id-btn" disabled={Boolean(pending)} onClick={() => setConfirmDelete(false)}>
                {cancellingBuild ? 'Keep building' : 'Keep deployment'}
              </button>
              <button
                type="button"
                className="id-btn id-btn--danger"
                disabled={Boolean(pending)}
                onClick={() => run(
                  retryingDelete ? 'retry-delete' : 'delete',
                  () => retryingDelete ? onRetry(instance.id) : onDelete(instance.id),
                )}
              >
                {pending
                  ? cancellingBuild ? 'Cancelling…' : 'Deleting…'
                  : retryingDelete ? 'Try deleting again' : cancellingBuild ? 'Cancel and remove' : 'Delete permanently'}
              </button>
            </div>
          </div>
        ) : (
          <button type="button" className="id-btn id-btn--danger" onClick={() => setConfirmDelete(true)}>
            <Trash width={16} /> {retryingDelete ? 'Try deleting again' : cancellingBuild ? 'Cancel deployment' : 'Delete deployment'}
          </button>
        )}

        <button ref={closeRef} type="button" className="id-btn id-modal-close" disabled={Boolean(pending)} onClick={onClose}>
          Close
        </button>
      </section>
    </div>
  )
}

function RailwayConnectionModal({
  token, connection, onClose, onReload, onChangeAccount, onDisconnected,
  connecting, connectionError,
}) {
  const [inventory, setInventory] = useState(null)
  const workspaceSequenceRef = useRef(0)
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  const [confirmDisconnect, setConfirmDisconnect] = useState(false)
  const closeRef = useRef(null)
  const busy = Boolean(pending) || connecting
  const dialogRef = useDialog(onClose, busy, closeRef)

  const reloadWorkspaces = useCallback(async () => {
    const sequence = ++workspaceSequenceRef.current
    setInventory(null)
    try {
      const data = await identityRequest(token, '/railway/workspaces')
      if (workspaceSequenceRef.current === sequence) setInventory(data)
    } catch {
      if (workspaceSequenceRef.current === sequence) {
        setInventory({ workspaces: [], current: null })
      }
    }
  }, [token])

  useEffect(() => {
    void reloadWorkspaces()
    return () => { workspaceSequenceRef.current += 1 }
  }, [reloadWorkspaces])

  const run = async (action, work) => {
    if (pending) return
    setPending(action)
    setError('')
    try {
      await work()
    } catch (requestError) {
      setError(requestError.message)
    } finally {
      setPending('')
    }
  }

  const workspaces = inventory?.workspaces || []
  const currentWorkspace = inventory?.current || ''

  return (
    <div className="id-modal-backdrop" onMouseDown={event => {
      if (!busy && event.target === event.currentTarget) onClose()
    }}>
      <section
        ref={dialogRef}
        className="id-modal id-manage-modal id-connection-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="railway-connection-title"
        aria-busy={busy}
        tabIndex={-1}
      >
        <div className="id-manage-head id-connection-head">
          <div>
            <h2 id="railway-connection-title">Railway account</h2>
            <p>{connection.account || 'Connected to Railway'}</p>
          </div>
          <button ref={closeRef} type="button" className="id-btn id-connection-close" disabled={busy} onClick={onClose}>
            Close
          </button>
        </div>

        <div className="id-connection-facts">
          {/* Render the workspace field from the first paint using the name the
             connection already carries, so it never pops in after the inventory
             fetch. It upgrades to an interactive picker only if more than one
             workspace is authorized. */}
          <div className="id-connection-fact">
            <span className="id-label">Workspace</span>
            {workspaces.length > 1 ? (
              <select
                className="id-select"
                value={currentWorkspace}
                disabled={busy}
                aria-label="Railway workspace"
                onChange={event => {
                  const nextId = event.target.value
                  run('workspace', async () => {
                    await identityRequest(token, '/railway/workspace', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ workspace_id: nextId }),
                    })
                    // Keep the picker in sync with the switch: loadRailway refreshes
                    // /railway (the header) but not this modal's workspace inventory.
                    setInventory(previous => (previous ? { ...previous, current: nextId } : previous))
                    await onReload()
                  })
                }}
              >
                {workspaces.map(item => (
                  <option key={item.id} value={item.id}>{item.name}</option>
                ))}
              </select>
            ) : (
              <span className="id-connection-value">{connection.workspace || 'Not selected'}</span>
            )}
          </div>
          <div className="id-connection-fact">
            <span className="id-label">Plan</span>
            <span className="id-connection-value">{planTitle(connection.plan) || 'Not detected yet'}</span>
            <div className="id-connection-plan-actions">
              <a className="id-btn id-connection-plan-link" href="https://railway.com/workspace/plans" target="_blank" rel="noopener noreferrer" aria-label="Manage Railway plan in a new tab">
                Manage plan on Railway <ArrowUpRight width={15} aria-hidden="true" />
              </a>
              <button
                type="button"
                className="id-btn id-connection-refresh"
                disabled={busy}
                aria-label="Refresh Railway plan"
                onClick={() => run('plan', async () => {
                  await identityRequest(token, '/railway/plan/refresh', { method: 'POST' })
                  await onReload()
                })}
              >
                {pending === 'plan' ? 'Refreshing…' : 'Refresh'}
              </button>
            </div>
          </div>
        </div>

        {connection.deploy_blocked && (
          <div className="id-manage-error">{connection.deploy_blocked}</div>
        )}
        {(error || connectionError) && <div className="id-signin-error" role="alert">{error || connectionError}</div>}

        {confirmDisconnect ? (
          <div className="id-delete-confirm">
            <strong>Disconnect Railway from Möbius?</strong>
            <span>New deployments will need a Railway account again. Existing deployments are unaffected.</span>
            <div>
              <button type="button" className="id-btn" disabled={busy} onClick={() => setConfirmDisconnect(false)}>
                Keep connected
              </button>
              <button
                type="button"
                className="id-btn id-btn--danger"
                disabled={busy}
                onClick={() => run('disconnect', async () => {
                  await identityRequest(token, '/railway/disconnect', { method: 'POST' })
                  onDisconnected()
                })}
              >
                {pending === 'disconnect' ? 'Disconnecting…' : 'Disconnect Railway'}
              </button>
            </div>
          </div>
        ) : (
          <div className="id-connection-footer">
            <button type="button" className="id-btn" disabled={busy} onClick={() => run('change', async () => {
              const next = await onChangeAccount()
              if (next) await reloadWorkspaces()
            })}>
              {connecting ? 'Connecting…' : 'Change hosting account'}
            </button>
            <button type="button" className="id-btn id-btn--quiet id-connection-disconnect" disabled={busy} aria-label="Disconnect Railway" onClick={() => setConfirmDisconnect(true)}>
              <Trash width={16} /> Disconnect
            </button>
          </div>
        )}
      </section>
    </div>
  )
}

function IdentityLoading() {
  return (
    <>
      <style>{IDENTITY_STYLES}</style>
      <main className="id-root id-root--settings" aria-busy="true">
        <span className="id-sr-only" role="status">Loading your account…</span>
        <div className="id-scroll">
          <div className="id-shell id-loading-layout" aria-hidden="true">
            <section className="id-hero id-loading-hero">
              <div className="id-skeleton id-loading-avatar" />
              <div className="id-loading-profile">
                <div className="id-skeleton id-loading-title" />
                <div className="id-skeleton id-loading-line" />
                <div className="id-skeleton id-loading-email" />
              </div>
            </section>
            <article className="id-card id-loading-card">
              <div className="id-card-head">
                <div>
                  <div className="id-skeleton id-loading-section-title" />
                  <div className="id-skeleton id-loading-line id-loading-line--short" />
                </div>
              </div>
              <div className="id-deployment">
                <div className="id-skeleton id-loading-deploy-mark" />
                <div>
                  <div className="id-skeleton id-loading-deploy-name" />
                  <div className="id-skeleton id-loading-line id-loading-line--deployment" />
                </div>
              </div>
            </article>
          </div>
        </div>
      </main>
    </>
  )
}

export default function IdentityAccount({ token }) {
  const queryClient = useQueryClient()
  const identityQuery = useIdentityQuery(token)
  const data = identityQuery.data ?? null
  const loading = identityQuery.isFetching
  const loadError = identityQuery.error?.message || ''
  const setData = next => publishIdentity(queryClient, next)
  const [railway, setRailway] = useState(null)
  const [railwayError, setRailwayError] = useState('')
  const [actionError, setActionError] = useState('')
  const [editing, setEditing] = useState(false)
  const [signingIn, setSigningIn] = useState(false)
  const [disconnecting, setDisconnecting] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)
  const [creatingDeployment, setCreatingDeployment] = useState(false)
  const [managingDeployment, setManagingDeployment] = useState(null)
  const [managingSection, setManagingSection] = useState(null)
  const [deletingDeployment, setDeletingDeployment] = useState(null)
  const [managingRailway, setManagingRailway] = useState(false)
  const [connectingRailway, setConnectingRailway] = useState(false)
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef(null)
  const railwaySequenceRef = useRef(0)
  const railwayConnectAbortRef = useRef(null)

  // Settings shares this read with its overview row; an explicit load always
  // asks the identity service again, and failures surface through the query.
  const load = useCallback(
    () => loadIdentity(queryClient, token, { force: true }).catch(() => {}),
    [queryClient, token],
  )

  const loadRailway = useCallback(async ({ quiet = false } = {}) => {
    const sequence = ++railwaySequenceRef.current
    if (!quiet) setRailwayError('')
    try {
      let next = await identityRequest(token, '/railway?region_options=1')
      // Region options are optional; without them Railway still answers on the
      // ordinary route instead of reporting its controls unavailable.
      if (next?.railway_access === 'unavailable') next = await identityRequest(token, '/railway')
      if (railwaySequenceRef.current === sequence) setRailway(next)
      return next
    } catch (requestError) {
      if (!quiet && railwaySequenceRef.current === sequence) {
        setRailwayError(requestError.message)
      }
      return null
    }
  }, [token])

  // Railway does not tell Möbius about plan changes, and the plan bounds the
  // resource choices, so re-check it once each time this page opens.
  const planCheckedRef = useRef(false)
  const railwayConnected = Boolean(railway?.connection?.connected)
  useEffect(() => {
    if (!railwayConnected || planCheckedRef.current) return
    planCheckedRef.current = true
    identityRequest(token, '/railway/plan/refresh', { method: 'POST' })
      .then(() => loadRailway({ quiet: true }))
      .catch(() => {})
  }, [railwayConnected, token, loadRailway])

  useEffect(() => {
    if (data?.account_mode === 'linked' || data?.account_mode === 'managed') {
      void loadRailway()
    } else {
      setRailway(null)
    }
    return () => { railwaySequenceRef.current += 1 }
  }, [data?.account_mode, loadRailway])

  const trackingRailway = (railway?.instances || []).some(deploymentNeedsTracking)
  useEffect(() => {
    if (!trackingRailway) return undefined
    let cancelled = false
    let timer
    const poll = async () => {
      await loadRailway({ quiet: true })
      if (!cancelled) timer = setTimeout(poll, 2500)
    }
    timer = setTimeout(poll, 1500)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [trackingRailway, loadRailway])

  useEffect(() => () => {
    railwayConnectAbortRef.current?.abort()
  }, [])

  /* The page keeps itself fresh instead of showing a refresh button: coming
     back to the tab (or the app pane regaining focus) quietly reloads. */
  const accountMode = data?.account_mode
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState !== 'visible') return
      void load()
      if (accountMode === 'linked' || accountMode === 'managed') {
        void loadRailway({ quiet: true })
      }
    }
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [load, loadRailway, accountMode])

  if (!data && loading) {
    return <IdentityLoading />
  }

  if (!data) {
    return (
      <>
        <style>{IDENTITY_STYLES}</style>
        <main className="id-root id-root--settings">
          <div className="id-scroll">
            <div className="id-shell">
              <section className="id-card id-fatal" role="alert">
                <h1>We couldn’t check your account.</h1>
                <p>{loadError}</p>
                <button type="button" className="id-btn" disabled={loading} onClick={load}>
                  {loading ? 'Trying again…' : 'Try again'}
                </button>
              </section>
            </div>
          </div>
        </main>
      </>
    )
  }

  const mode = data.account_mode
  const unavailable = data.account_unavailable
  const profile = data.profile
  const canEdit = (mode === 'linked' || mode === 'managed')
    && !unavailable
    && Boolean(profile)
  const needsHandle = canEdit && !profile?.handle
  const activeDeployments = data.deployments
    .filter(item => deploymentPresentation(item).tone === 'success')
    .length
  const memberSince = formatMembershipMonth(data.member_since)

  const saveHandle = async handle => {
    setActionError('')
    setData(await identityRequest(token, '/profile', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ handle }),
    }))
  }

  const uploadAvatar = async event => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || !canEdit) return
    setUploading(true)
    setActionError('')
    try {
      const body = new FormData()
      body.append('avatar', file)
      publishIdentity(
        queryClient,
        await identityRequest(token, '/avatar', { method: 'POST', body }),
        { avatarChanged: true },
      )
    } catch (requestError) {
      setActionError(requestError.message)
    } finally {
      setUploading(false)
    }
  }

  const railwayAction = async (path, options = {}) => {
    const result = await identityRequest(token, `/railway${path}`, options)
    await loadRailway()
    return result
  }

  const connectRailway = async (replace = false) => {
    if (connectingRailway) return
    setConnectingRailway(true)
    setRailwayError('')
    const previousAccount = replace ? railway?.connection?.account : null
    const popup = window.open('about:blank', 'mobius-railway-connect', 'width=560,height=760')
    if (!popup) {
      setConnectingRailway(false)
      setRailwayError('Your browser blocked the Railway window. Allow popups, then try again.')
      return
    }
    const controller = new AbortController()
    railwayConnectAbortRef.current = controller
    try {
      const started = await identityRequest(token, '/railway/connect/start', {
        method: 'POST',
        signal: controller.signal,
        ...(replace
          ? {
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ replace: true }),
          }
          : {}),
      })
      const authorization = new URL(started.authorization_url)
      if (authorization.protocol !== 'https:') throw new Error('Möbius returned an invalid Railway sign-in address.')
      popup.location.replace(authorization.href)
      const deadline = Date.now() + 10 * 60 * 1000
      while (!controller.signal.aborted && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 900))
        const next = await loadRailway({ quiet: true })
        if (replace ? railwayAccountChanged(previousAccount, next) : next?.connection?.connected) {
          try { popup.close() } catch { /* already closed */ }
          return next
        }
        if (popup.closed) {
          if (replace && next?.connection?.connected) {
            throw new Error('Railway returned the same account. Sign out of Railway in your browser, then try changing the account again.')
          }
          throw new Error('Railway connection was cancelled. Try again when you are ready.')
        }
      }
      throw new Error('Railway connection took too long. Please try again.')
    } catch (requestError) {
      if (!controller.signal.aborted) setRailwayError(requestError.message)
      try { popup.close() } catch { /* already closed */ }
    } finally {
      if (railwayConnectAbortRef.current === controller) railwayConnectAbortRef.current = null
      setConnectingRailway(false)
    }
  }

  return (
    <>
      <style>{IDENTITY_STYLES}</style>
      <main className="id-root id-root--settings">
        <div className="id-scroll">
          <div className="id-shell">

          {loadError && (
            <section className="id-notice id-notice--error" role="alert">
              <strong>We couldn’t refresh your account.</strong>
              <span>{loadError} The last confirmed details remain below.</span>
              <button type="button" className="id-btn" disabled={loading} onClick={load}>
                {loading ? 'Trying again…' : 'Try again'}
              </button>
            </section>
          )}

          {unavailable && (
            <section className="id-notice" role="status">
              <strong>
                Your {mode === 'managed' ? 'managed account' : 'linked account'} is temporarily unavailable.
              </strong>
              <span>
                This Möbius is still available. Account details and editing are paused until the connection recovers.
              </span>
            </section>
          )}


          {railwayError && (
            <section className="id-notice id-notice--error" role="alert">
              <strong>Railway controls need attention.</strong>
              <span>{railwayError}</span>
              <button type="button" className="id-btn" onClick={() => loadRailway()}>
                Try again
              </button>
            </section>
          )}

          {mode === 'signed_out' ? (
            <>
              <section className="id-auth">
                <h1>Your account stays yours.</h1>
                <p>
                  Sign in to see your profile and deployments. Until then, account details stay
                  hidden and this installation remains self-hosted.
                </p>
                <button
                  type="button"
                  className="id-btn id-btn--primary id-auth-button"
                  onClick={() => setSigningIn(true)}
                >
                  Sign in to Möbius
                </button>
              </section>
              <Deployments
                token={token}
                items={data.deployments}
                railway={railway}
                selfHosted={mode === 'linked'}
              />
            </>
          ) : (
            <>
              <section className="id-hero">
                <IdentityCard
                  footer={(
                    <div className="id-cardfoot">
                      {memberSince && (
                        <span className="id-cardkv">
                          Member since
                          <b>{memberSince}</b>
                        </span>
                      )}
                      <span className="id-cardkv">
                        Deployments
                        <b>{`${data.deployments.length} ${activeDeployments === data.deployments.length ? 'active' : `· ${activeDeployments} active`}`}</b>
                      </span>
                      {mode === 'linked' && !unavailable && (
                        <span className="id-cardlink">
                          <span className="id-dot id-dot--online" aria-hidden="true" />
                          mobius.you
                        </span>
                      )}
                    </div>
                  )}
                >
                  <div className="id-cardid">
                    <div className={`id-avatar${!canEdit ? ' is-disabled' : ''}`}>
                      <ProfileAvatar profile={profile} token={token} />
                      {canEdit && (
                        <>
                          <button
                            type="button"
                            className="id-avatar-edit"
                            disabled={uploading}
                            aria-label={uploading ? 'Uploading profile picture' : 'Change profile picture'}
                            onClick={() => fileRef.current?.click()}
                          >
                            {uploading
                              ? <ArrowRotateCw className="id-spin" width={16} />
                              : <Camera width={16} />}
                          </button>
                          <input
                            ref={fileRef}
                            hidden
                            type="file"
                            accept="image/png,image/jpeg,image/webp"
                            onChange={uploadAvatar}
                          />
                        </>
                      )}
                    </div>
                    <div className="id-profile-copy">
                      <div className="id-title-row">
                        <h1 className="id-title">
                          {profile?.handle
                            ? `@${profile.handle}`
                            : unavailable
                              ? 'Account unavailable'
                              : 'Choose your handle'}
                        </h1>
                        {canEdit && (
                          <button
                            type="button"
                            className="id-handle-btn"
                            aria-label="Change handle"
                            onClick={() => setEditing(true)}
                          >
                            <Pencil width={18} />
                          </button>
                        )}
                      </div>
                      {/* Möbius sign-in does not share its email with this instance, so
                          the card falls back to the connected Railway account's email. */}
                      {(profile?.email || railway?.connection?.account) && (
                        <div className="id-email">
                          <Lock width={13} aria-hidden="true" />
                          <span>{profile?.email || railway.connection.account}</span>
                          <span className="id-private-label">Private</span>
                        </div>
                      )}
                    </div>
                  </div>
                </IdentityCard>
              </section>
              {mode === 'linked' && !unavailable && (
                <button type="button" className="id-btn id-btn--quiet id-settings-unlink" onClick={() => setDisconnecting(true)}>Disconnect Möbius account</button>
              )}

              <Deployments
                token={token}
                items={data.deployments}
                railway={railway}
                selfHosted={mode === 'linked'}
                onNew={() => setCreatingDeployment(true)}
                managingDeployment={managingDeployment}
                managingSection={managingSection}
                onCloseManage={() => setManagingDeployment(null)}
                onManage={(instance, section = null) => {
                  if (managingDeployment?.id === instance.id && managingSection === section) {
                    setManagingDeployment(null)
                    return
                  }
                  setManagingSection(section)
                  setManagingDeployment(instance)
                }}
                onCompute={(id, payload) => railwayAction(`/deployments/${id}/compute`, {
                  method: 'PATCH',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify(payload),
                })}
                onStorage={(id, payload) => railwayAction(`/deployments/${id}/storage`, {
                  method: 'PATCH',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify(payload),
                })}
                onRetry={id => railwayAction(`/deployments/${id}/retry`, { method: 'POST' })}
                onDelete={setDeletingDeployment}
                onRename={(id, payload) => railwayAction(`/deployments/${id}`, {
                  method: 'PATCH',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify(payload),
                })}
                onConnect={() => connectRailway()}
                connecting={connectingRailway}
                onManageConnection={() => setManagingRailway(true)}
                onReconnect={() => {
                  setReconnecting(true)
                  setDisconnecting(true)
                }}
              />

            </>
          )}

          {actionError && <div className="id-error" role="alert">{actionError}</div>}
          </div>
        </div>

        {signingIn && (
          <SignInModal
            token={token}
            onClose={() => setSigningIn(false)}
            onSignedIn={next => {
              setData(next)
              setSigningIn(false)
              setReconnecting(false)
              void loadRailway()
            }}
          />
        )}
        {(editing || needsHandle) && canEdit && (
          <HandleModal
            current={profile?.handle}
            required={needsHandle}
            onClose={() => { if (!needsHandle) setEditing(false) }}
            onSave={saveHandle}
          />
        )}
        {disconnecting && mode === 'linked' && (
          <DisconnectModal
            token={token}
            reconnecting={reconnecting}
            onClose={() => setDisconnecting(false)}
            onDisconnected={() => {
              setDisconnecting(false)
              if (reconnecting) {
                void load()
                setSigningIn(true)
              } else {
                void load()
              }
            }}
          />
        )}
        {creatingDeployment && (
          <NewDeploymentModal
            planLimits={railway?.connection?.plan_limits}
            plan={railway?.connection?.plan}
            regionOptions={railway?.connection?.region_options}
            onClose={() => setCreatingDeployment(false)}
            onCreate={payload => railwayAction('/deployments', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            })}
          />
        )}
        {deletingDeployment && (
          <DeleteDeploymentModal
            instance={deletingDeployment}
            token={token}
            onClose={() => setDeletingDeployment(null)}
            onRetry={id => railwayAction(`/deployments/${id}/retry`, { method: 'POST' })}
            onDelete={id => railwayAction(`/deployments/${id}`, { method: 'DELETE' })}
            onConfirmAbsent={id => railwayAction(`/deployments/${id}/confirm-absent`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ confirmed_absent: true }),
            })}
          />
        )}
        {managingRailway && railway?.connection && (
          <RailwayConnectionModal
            token={token}
            connection={railway.connection}
            onClose={() => setManagingRailway(false)}
            onReload={loadRailway}
            onChangeAccount={() => connectRailway(true)}
            connecting={connectingRailway}
            connectionError={railwayError}
            onDisconnected={() => {
              setManagingRailway(false)
              void loadRailway()
            }}
          />
        )}
      </main>
    </>
  )
}
