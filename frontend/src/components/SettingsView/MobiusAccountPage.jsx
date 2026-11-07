/* Settings › Accounts › Möbius: sign in, @handle and photo, every deployment on the account, and disconnect; model credit and visibility (MobiusModelAccess) render on the Möbius provider page. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import {
  ArrowRotateCw,
  ArrowUpRight,
  Camera,
  ChevronDown,
  ExternalLink,
  Lock,
  Pencil,
  Sparkle,
  Warning,
  X,
} from '@openai/apps-sdk-ui/components/Icon'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import useMobiusSignIn from '../../hooks/useMobiusSignIn.js'
import { authQueries, modelQueries } from '../../hooks/queries.js'
import useAuthedImageUrl, { forgetAuthedImage } from '../../hooks/useAuthedImageUrl.js'
import { AVATAR_PATH, mobiusAccountQueries } from '../../hooks/mobiusAccountQueries.js'
import {
  AVATAR_MAX_BYTES,
  AVATAR_TYPES,
  HANDLE_PATTERN,
  deploymentCanOpen,
  deploymentCanRecover,
  deploymentIsBuilding,
  deploymentNeedsTracking,
  deploymentPresentation,
  formatCredit,
  formatMembershipMonth,
  formatTokenRate,
  mergeDeployments,
  meterPercent,
  mobiusAccessRequest,
  parseDeploymentMetrics,
  railwayAccountChanged,
  resourceSummary,
} from '../../lib/mobiusAccount.js'
import { AppleMark, GoogleMark } from '../ui/AccountProviderMarks.jsx'
import './SettingsPage.css'
import './MobiusAccountPage.css'

const TONE_BADGE = {
  success: 'settings-badge--success',
  danger: 'settings-badge--danger',
  progress: 'settings-badge--accent',
  muted: '',
}
const DASHBOARD_MISSING = 'Update Möbius to open your account dashboard from here.'

const json = body => ({
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

function avatarProblem(file) {
  if (!AVATAR_TYPES.includes(file.type)) return 'Choose a JPEG, PNG or WebP image.'
  if (file.size > AVATAR_MAX_BYTES) return 'Choose an image under 5 MB.'
  return ''
}

function initial(profile) {
  return (profile?.handle || profile?.email || 'M').slice(0, 1).toUpperCase()
}

function Spinner({ size = 14 }) {
  return <ArrowRotateCw className="mobius-account__spin" width={size} height={size} aria-hidden="true" />
}

/* A protected deployment action: always a link to the signed-in account
   dashboard, never a local mutation. Without a dashboard address the action
   stays visible but unavailable, with the reason. */
function DashboardLink({ url, label, ariaLabel, danger = false }) {
  const className = `settings__btn settings__btn--sm ${danger ? 'settings__btn--danger' : 'settings__btn--outline'} mobius-account__btn`
  if (!url) {
    return <button type="button" className={className} disabled title={DASHBOARD_MISSING}>{label}</button>
  }
  return (
    <a className={className} href={url} target="_blank" rel="noopener noreferrer" aria-label={`${ariaLabel || label} on mobius.you (opens in a new tab)`}>
      {label}<ExternalLink width={13} height={13} aria-hidden="true" />
    </a>
  )
}

function ErrorMessage({ error, onRetry, retryLabel = 'Try again' }) {
  if (!error) return null
  const message = typeof error === 'string' ? error : error.message
  return (
    <p className="settings-message settings-message--error mobius-account__message" role="alert">
      <span>{message}</span>
      {error.confirmUrl && <a className="settings-link" href={error.confirmUrl} target="_blank" rel="noopener noreferrer">Open mobius.you</a>}
      {onRetry && <button type="button" className="mobius-account__text-btn" onClick={onRetry}>{retryLabel}</button>}
    </p>
  )
}

function SkeletonList({ rows = 2 }) {
  return (
    <div className="settings-list" aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <div className="settings-item" key={index}>
          <span className="settings-item__icon mobius-account__skeleton" />
          <span className="settings-item__body">
            <span className="mobius-account__skeleton mobius-account__skeleton--line" />
            <span className="mobius-account__skeleton mobius-account__skeleton--short" />
          </span>
        </div>
      ))}
    </div>
  )
}

function GroupHead({ id, title, desc, aside }) {
  return (
    <div className="settings-group__head">
      <div className="settings-group__heading">
        <h2 className="settings-group__title" id={id}>{title}</h2>
        {desc && <p className="settings-group__desc">{desc}</p>}
      </div>
      {aside && <div className="settings-group__aside">{aside}</div>}
    </div>
  )
}

// ── Sign in ───────────────────────────────────────────────────────────────

function SignInSection({ onSignedIn, reconnecting }) {
  const signIn = useMobiusSignIn({ onSignedIn })
  const { pending } = signIn
  const waiting = pending === 'google' || pending === 'apple'
  const providers = [
    { id: 'google', label: 'Continue with Google', Mark: GoogleMark },
    { id: 'apple', label: 'Continue with Apple', Mark: AppleMark },
  ]
  const progress = pending === 'complete'
    ? 'Finishing sign-in…'
    : waiting ? `Waiting for ${pending === 'google' ? 'Google' : 'Apple'}… Finish in the window that opened.` : ''
  return (
    <section className="settings-group" aria-labelledby="mobius-account-signin">
      <GroupHead
        id="mobius-account-signin"
        title={reconnecting ? 'Sign in again' : 'Sign in to Möbius'}
        desc={reconnecting
          ? 'Sign in once more to approve deployment management. Your account and deployments are unchanged.'
          : 'Use your mobius.you account to claim a @handle, use Möbius models and see all your deployments here. This Möbius keeps working either way.'}
      />
      <div className="settings-list">
        {providers.map(({ id, label, Mark }) => (
          <button
            key={id}
            type="button"
            className="settings-item"
            disabled={Boolean(pending)}
            aria-busy={pending === id || undefined}
            onClick={() => signIn.begin(id)}
          >
            <span className="settings-item__icon mobius-account__provider-mark"><Mark /></span>
            <span className="settings-item__body"><span className="settings-item__title">{label}</span></span>
            <span className="settings-item__trail">
              {pending === id ? <Spinner /> : <ArrowUpRight width={16} height={16} aria-hidden="true" />}
            </span>
          </button>
        ))}
        {(progress || signIn.canRetry) && (
          <div className="settings-item-panel mobius-account__signin-status">
            {progress && <p className="settings-item-panel__text" role="status" aria-live="polite">{progress}</p>}
            <div className="settings-actions">
              {signIn.canRetry && (
                <button type="button" className="settings__btn settings__btn--sm" disabled={Boolean(pending)} onClick={signIn.retry}>
                  Finish signing in
                </button>
              )}
              {(waiting || signIn.canRetry) && (
                <button type="button" className="settings__btn settings__btn--sm settings__btn--outline" onClick={signIn.cancel}>
                  Cancel
                </button>
              )}
            </div>
          </div>
        )}
      </div>
      <ErrorMessage error={signIn.error} />
      <p className="settings-group__foot">
        A sign-in window opens and closes by itself when you’re done. Your password and keys never pass through this page.
      </p>
    </section>
  )
}

// ── Profile ───────────────────────────────────────────────────────────────

function HandleDialog({ current, onClose, onSave }) {
  const [value, setValue] = useState(current || '')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const dialogRef = useRef(null)
  const inputRef = useRef(null)
  const valid = HANDLE_PATTERN.test(value)
  const close = useCallback(() => { if (!pending) onClose() }, [pending, onClose])
  useDialogFocus({ containerRef: dialogRef, initialFocusRef: inputRef, onClose: close })

  const submit = async event => {
    event.preventDefault()
    if (!valid || pending || value === current) return
    setPending(true)
    setError('')
    try {
      await onSave(value)
      onClose()
    } catch (saveError) {
      setError(saveError.message)
      setPending(false)
    }
  }

  return (
    <div className="mobius-account__overlay" onMouseDown={event => { if (event.target === event.currentTarget) close() }}>
      <form
        ref={dialogRef}
        className="mobius-account__dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="mobius-handle-title"
        aria-describedby="mobius-handle-desc"
        aria-busy={pending}
        onSubmit={submit}
      >
        <div className="mobius-account__dialog-head">
          <h2 id="mobius-handle-title">Change your handle</h2>
          <button type="button" className="mobius-account__icon-btn" aria-label="Close" disabled={pending} onClick={close}>
            <X width={18} height={18} aria-hidden="true" />
          </button>
        </div>
        <p id="mobius-handle-desc" className="mobius-account__dialog-text">
          Your handle is how people find you across Möbius. Your email stays private.
        </p>
        <HandleField inputRef={inputRef} value={value} disabled={pending} invalid={Boolean(error)} onChange={next => { setValue(next); setError('') }} />
        <p className={`mobius-account__hint${error ? ' is-error' : ''}`} id="mobius-handle-hint" role={error ? 'alert' : undefined}>
          {error || (valid ? 'We’ll confirm it’s available when you save.' : 'Use 3–30 lowercase letters, numbers or underscores.')}
        </p>
        <div className="settings-actions mobius-account__dialog-actions">
          <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" disabled={pending} onClick={close}>Cancel</button>
          <button type="submit" className="settings__btn settings__btn--sm" disabled={!valid || pending || value === current}>
            {pending ? 'Saving…' : 'Save handle'}
          </button>
        </div>
      </form>
    </div>
  )
}

function HandleField({ inputRef, value, disabled, invalid, onChange, id = 'mobius-handle-input' }) {
  return (
    <div className={`mobius-account__field${invalid ? ' is-invalid' : ''}`}>
      <span aria-hidden="true">@</span>
      <input
        ref={inputRef}
        id={id}
        value={value}
        maxLength={30}
        placeholder="yourname"
        autoComplete="username"
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
        aria-label="Handle"
        aria-invalid={invalid || undefined}
        aria-describedby="mobius-handle-hint"
        disabled={disabled}
        onChange={event => onChange(event.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ''))}
      />
    </div>
  )
}

/* An account without a handle claims one before the rest of its profile. */
function ClaimHandle({ onSave }) {
  const [value, setValue] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const valid = HANDLE_PATTERN.test(value)
  const submit = async event => {
    event.preventDefault()
    if (!valid || pending) return
    setPending(true)
    setError('')
    try {
      await onSave(value)
    } catch (saveError) {
      setError(saveError.message)
    } finally {
      setPending(false)
    }
  }
  return (
    <form className="settings-item settings-item--block mobius-account__claim" onSubmit={submit} aria-busy={pending}>
      <div className="settings-item__body">
        <span className="settings-item__title">Choose your handle</span>
        <span className="settings-item__sub settings-item__sub--wrap">Your handle is your identity across Möbius. Claim one to finish your profile.</span>
      </div>
      <div className="mobius-account__claim-row">
        <HandleField value={value} disabled={pending} invalid={Boolean(error)} onChange={next => { setValue(next); setError('') }} />
        <button type="submit" className="settings__btn settings__btn--sm" disabled={!valid || pending}>{pending ? 'Claiming…' : 'Claim handle'}</button>
      </div>
      <p className={`mobius-account__hint${error ? ' is-error' : ''}`} id="mobius-handle-hint" role={error ? 'alert' : undefined}>
        {error || 'Use 3–30 lowercase letters, numbers or underscores.'}
      </p>
    </form>
  )
}

function ProfileSection({ identity, onIdentity }) {
  const profile = identity.profile
  const avatarUrl = useAuthedImageUrl(AVATAR_PATH, profile.avatar_url || '').url
  const [editing, setEditing] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const fileRef = useRef(null)
  const memberSince = formatMembershipMonth(identity.member_since)

  const saveHandle = async handle => {
    onIdentity(await mobiusAccessRequest('/api/identity/profile', { method: 'PATCH', ...json({ handle }) }))
  }

  const upload = async event => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    const problem = avatarProblem(file)
    if (problem) { setError(problem); return }
    setUploading(true)
    setError('')
    try {
      const body = new FormData()
      body.append('avatar', file)
      const next = await mobiusAccessRequest('/api/identity/avatar', { method: 'POST', body })
      // The account may keep the same avatar address for new bytes.
      forgetAuthedImage(AVATAR_PATH)
      onIdentity(next)
    } catch (uploadError) {
      setError(uploadError.message)
    } finally {
      setUploading(false)
    }
  }

  return (
    <section className="settings-group" aria-labelledby="mobius-account-profile">
      <GroupHead id="mobius-account-profile" title="Profile" desc="Your handle is your public identity. Your email stays private." />
      <div className="settings-list">
        {profile.handle ? (
          <div className="settings-item">
            <span className="settings-item__icon settings-item__icon--round mobius-account__avatar">
              {avatarUrl ? <img src={avatarUrl} alt="" /> : <span aria-hidden="true">{initial(profile)}</span>}
            </span>
            <span className="settings-item__body">
              <span className="settings-item__title">Photo</span>
              <span className="settings-item__sub settings-item__sub--wrap">Saved to your Möbius account only when you upload one.</span>
            </span>
            <span className="settings-item__trail">
              <button type="button" className="settings__btn settings__btn--outline settings__btn--sm mobius-account__btn" disabled={uploading} onClick={() => fileRef.current?.click()}>
                {uploading ? <><Spinner /> Uploading…</> : <><Camera width={14} height={14} aria-hidden="true" />{profile.avatar_url ? 'Change' : 'Upload'}</>}
              </button>
              <input ref={fileRef} type="file" accept={AVATAR_TYPES.join(',')} hidden onChange={upload} />
            </span>
          </div>
        ) : null}
        {profile.handle ? (
          <div className="settings-item">
            <span className="settings-item__body">
              <span className="settings-item__title">Handle</span>
              <span className="settings-item__sub">@{profile.handle}</span>
            </span>
            <span className="settings-item__trail">
              <button type="button" className="settings__btn settings__btn--outline settings__btn--sm mobius-account__btn" onClick={() => setEditing(true)} aria-label="Change handle">
                <Pencil width={14} height={14} aria-hidden="true" />Change
              </button>
            </span>
          </div>
        ) : <ClaimHandle onSave={saveHandle} />}
        {profile.email && (
          <div className="settings-item">
            <span className="settings-item__body">
              <span className="settings-item__title">Email</span>
              <span className="settings-item__sub">{profile.email}</span>
            </span>
            <span className="settings-item__trail">
              <span className="settings-badge settings-badge--plain"><Lock width={12} height={12} aria-hidden="true" />Private</span>
            </span>
          </div>
        )}
        {memberSince && (
          <div className="settings-item">
            <span className="settings-item__body"><span className="settings-item__title">Member since</span></span>
            <span className="settings-item__trail"><span className="settings-item__value">{memberSince}</span></span>
          </div>
        )}
      </div>
      <ErrorMessage error={error} />
      {editing && <HandleDialog current={profile.handle} onClose={() => setEditing(false)} onSave={saveHandle} />}
    </section>
  )
}

// ── Model access ──────────────────────────────────────────────────────────

function ModelVisibilityRow() {
  const queryClient = useQueryClient()
  const visibility = mobiusAccountQueries.visibility.useQuery()
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const enabled = visibility.data
  const change = async next => {
    if (saving) return
    setSaving(true)
    setError('')
    try {
      const result = await mobiusAccessRequest('/api/auth/providers/mobius/enabled', { method: 'PATCH', ...json({ enabled: next }) })
      queryClient.setQueryData(mobiusAccountQueries.visibility.key, result.enabled === true)
      authQueries.provider.statuses.invalidate(queryClient)
      modelQueries.registry.invalidate(queryClient)
    } catch (changeError) {
      setError(changeError.message)
    } finally {
      setSaving(false)
    }
  }
  return (
    <>
      <div className="settings-item">
        <span className="settings-item__body">
          <label className="settings-item__title settings-item__title--wrap" htmlFor="mobius-models-visible">Show Möbius models in pickers</label>
          <span className="settings-item__sub settings-item__sub--wrap">
            {enabled === false
              ? 'Existing Möbius chats stay saved but can’t run until you turn this back on.'
              : 'On by default. You can change this without signing in.'}
          </span>
        </span>
        <span className="settings-item__trail">
          <Switch className="settings-switch" id="mobius-models-visible" checked={enabled === true} disabled={typeof enabled !== 'boolean' || saving} onCheckedChange={change} />
        </span>
      </div>
      {(error || visibility.error) && (
        <div className="settings-item-panel">
          <ErrorMessage error={error || visibility.error} onRetry={error ? null : () => visibility.refetch()} />
        </div>
      )}
    </>
  )
}

/* `account` is 'loading', 'signed_out', 'outage' (signed in, account service unreachable) or 'ready'. */
function ModelAccessSection({ account, onSignIn = null }) {
  const queryClient = useQueryClient()
  const agent = mobiusAccountQueries.agent.useQuery({ enabled: account === 'ready' })
  const [confirming, setConfirming] = useState(false)
  const [activating, setActivating] = useState(false)
  const [error, setError] = useState('')
  const [pricesOpen, setPricesOpen] = useState(false)
  const access = agent.data

  const activate = async () => {
    if (activating) return
    setActivating(true)
    setError('')
    try {
      queryClient.setQueryData(mobiusAccountQueries.agent.key, await mobiusAccessRequest('/api/identity/agent/trial', { method: 'POST' }))
      authQueries.provider.statuses.invalidate(queryClient)
      setConfirming(false)
    } catch (activateError) {
      setError(activateError.code === 'trial_fund_exhausted'
        ? 'The shared trial fund is empty right now. Your account is ready; the Möbius team can add more trial capacity.'
        : activateError.message)
    } finally {
      setActivating(false)
    }
  }

  const loadingRow = <div className="settings-item" aria-busy="true"><span className="settings-item__body"><span className="mobius-account__skeleton mobius-account__skeleton--line" /></span></div>
  let accessRows = null
  if (account === 'loading') {
    accessRows = loadingRow
  } else if (account === 'signed_out' || (account === 'ready' && access?.agent_access === 'signed_out')) {
    accessRows = (
      <div className="settings-item">
        <span className="settings-item__icon"><Sparkle width={18} height={18} aria-hidden="true" /></span>
        <span className="settings-item__body">
          <span className="settings-item__title">Möbius models</span>
          <span className="settings-item__sub settings-item__sub--wrap">Sign in to use Möbius models and start your free trial credit.</span>
        </span>
        <span className="settings-item__trail">
          {onSignIn
            ? <button type="button" className="settings__btn settings__btn--sm" onClick={onSignIn}>Sign in</button>
            : <span className="settings-badge">Sign in needed</span>}
        </span>
      </div>
    )
  } else if (account === 'outage' || access?.agent_access === 'unavailable') {
    accessRows = (
      <div className="settings-item">
        <span className="settings-item__body">
          <span className="settings-item__title">Model access</span>
          <span className="settings-item__sub settings-item__sub--wrap">Temporarily unavailable. Your account is safe; check again in a moment.</span>
        </span>
        <span className="settings-item__trail"><span className="settings-badge settings-badge--warning">Unavailable</span></span>
      </div>
    )
  } else if (!access) {
    accessRows = agent.isPending
      ? loadingRow
      : (
        <div className="settings-item">
          <span className="settings-item__body">
            <span className="settings-item__title">Model access</span>
            <span className="settings-item__sub settings-item__sub--wrap">Model access couldn’t be checked just now. Your account is safe.</span>
          </span>
          <span className="settings-item__trail">
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => agent.refetch()}>Try again</button>
          </span>
        </div>
      )
  } else {
    const trialReady = access.trial.state === 'ready'
    const empty = !trialReady && access.balance.available_units <= 0
    accessRows = <>
      <div className="settings-item">
        <span className="settings-item__icon"><Sparkle width={18} height={18} aria-hidden="true" /></span>
        <span className="settings-item__body">
          <span className="settings-item__title">{trialReady ? 'Start with $2 on us' : 'Model credit'}</span>
          <span className="settings-item__sub settings-item__sub--wrap">
            {trialReady
              ? 'Activate your trial credit to start using Möbius models.'
              : empty ? 'Your credit has run out. Ask your Möbius provider to add more.' : 'Available for Möbius models in chats and agents.'}
          </span>
        </span>
        <span className="settings-item__trail">
          {trialReady
            ? !confirming && <button type="button" className="settings__btn settings__btn--sm" onClick={() => setConfirming(true)}>Activate…</button>
            : empty
              ? <span className="settings-badge settings-badge--warning">No credit</span>
              : <span className="settings-item__value mobius-account__credit">{formatCredit(access.balance.available_units)}</span>}
        </span>
      </div>
      {trialReady && confirming && (
        <div className="settings-item-panel">
          <p className="settings-item-panel__text">Activate your $2 trial credit now? The trial starts as soon as you activate it.</p>
          <div className="settings-actions">
            <button type="button" className="settings__btn settings__btn--sm" disabled={activating} onClick={activate}>{activating ? 'Activating…' : 'Activate trial'}</button>
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" disabled={activating} onClick={() => setConfirming(false)}>Cancel</button>
          </div>
        </div>
      )}
      <div className="settings-item settings-item--top">
        <span className="settings-item__icon"><Lock width={16} height={16} aria-hidden="true" /></span>
        <span className="settings-item__body">
          <span className="settings-item__title">Privacy during testing</span>
          <span className="settings-item__sub settings-item__sub--wrap">{access.retention.notice}</span>
        </span>
      </div>
      <button type="button" className="settings-item" aria-expanded={pricesOpen} aria-controls="mobius-model-rates" onClick={() => setPricesOpen(open => !open)}>
        <span className="settings-item__body">
          <span className="settings-item__title">Models and token rates</span>
          <span className="settings-item__sub">{access.models.length} {access.models.length === 1 ? 'model' : 'models'} available</span>
        </span>
        <ChevronDown className="settings-item__chevron" width={16} height={16} aria-hidden="true" />
      </button>
      {pricesOpen && (
        <div className="settings-item-panel" id="mobius-model-rates">
          <ul className="mobius-account__rates" aria-label="Möbius models and prices per million tokens">
            {access.models.map(model => (
              <li key={model.id}>
                <strong>{model.name}</strong>
                <span>{formatTokenRate(model.pricing.input)} in · {formatTokenRate(model.pricing.cached_input)} cached · {formatTokenRate(model.pricing.output)} out</span>
              </li>
            ))}
          </ul>
          <p className="settings-item-panel__text mobius-account__rates-unit">Prices are USD per million tokens.</p>
        </div>
      )}
    </>
  }

  return (
    <section className="settings-group" aria-labelledby="mobius-account-models">
      <GroupHead id="mobius-account-models" title="Möbius models" desc="Credit for the models Möbius runs for you, and where they appear." />
      <div className="settings-list">
        {accessRows}
        <ModelVisibilityRow />
      </div>
      <ErrorMessage error={error} />
    </section>
  )
}

/**
 * Möbius model credit, trial, picker visibility and rates, for the Möbius
 * provider page. `onSignIn` opens the account page when the owner is signed out.
 */
export function MobiusModelAccess({ onSignIn = null }) {
  const identityQuery = mobiusAccountQueries.identity.useQuery()
  const identity = identityQuery.data
  const mode = identity?.account_mode
  // Until the account is known the section waits quietly instead of asking a
  // signed-in owner to sign in; a failed first read is an outage, not sign-out.
  const account = !identity
    ? identityQuery.isPending ? 'loading' : 'outage'
    : mode === 'signed_out' ? 'signed_out' : identity.account_unavailable ? 'outage' : 'ready'
  return <ModelAccessSection account={account} onSignIn={onSignIn} />
}

/* The account page points to model credit on the Möbius provider page rather than repeating it. */
function ModelsLink({ onOpenModels }) {
  if (!onOpenModels) return null
  return (
    <section className="settings-group" aria-labelledby="mobius-account-models-link">
      <GroupHead id="mobius-account-models-link" title="Möbius models" />
      <div className="settings-list">
        <button type="button" className="settings-item" onClick={onOpenModels}>
          <span className="settings-item__icon"><Sparkle width={18} height={18} aria-hidden="true" /></span>
          <span className="settings-item__body">
            <span className="settings-item__title">Credit, trial and models</span>
            <span className="settings-item__sub">Usage, token rates and where Möbius models appear</span>
          </span>
        </button>
      </div>
    </section>
  )
}

// ── Deployments ───────────────────────────────────────────────────────────

function MetricMeter({ label, value, limit, percent }) {
  const pct = meterPercent(percent)
  const tone = pct >= 90 ? ' settings-meter--danger' : pct >= 75 ? ' settings-meter--warning' : ''
  return (
    <div className={`settings-meter${tone}`}>
      <div className="settings-meter__label">
        <span>{label}</span>
        <span className="settings-meter__value">{value || '—'}{limit ? <span className="mobius-account__meter-limit"> / {limit}</span> : null}</span>
      </div>
      <div className="settings-meter__track"><div className="settings-meter__fill" style={{ width: `${pct}%` }} /></div>
    </div>
  )
}

function formatDeployedAt(iso) {
  const then = new Date(iso || '')
  if (!iso || Number.isNaN(then.getTime())) return ''
  const mins = Math.round((Date.now() - then.getTime()) / 60000)
  if (mins < 1) return 'Deployed just now'
  if (mins < 60) return `Deployed ${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `Deployed ${hrs}h ago`
  const days = Math.round(hrs / 24)
  if (days < 30) return `Deployed ${days}d ago`
  return `Deployed ${then.toLocaleDateString()}`
}

/* Live metrics for one ready deployment, refreshed every 15 s while the page is
   visible. Only the newest request schedules the next one. */
function DeploymentMetrics({ instanceId, active }) {
  const [metrics, setMetrics] = useState(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    if (!active) return undefined
    let cancelled = false
    let controller = null
    let timer = null
    const refresh = async () => {
      if (document.visibilityState !== 'visible') return
      controller?.abort()
      const request = new AbortController()
      controller = request
      try {
        const body = await mobiusAccessRequest(`/api/identity/railway/deployments/${encodeURIComponent(instanceId)}/metrics?cost=1`, { signal: request.signal })
        if (!cancelled && !request.signal.aborted) { setMetrics(parseDeploymentMetrics(body)); setFailed(false) }
      } catch {
        if (!cancelled && !request.signal.aborted) setFailed(true)
      } finally {
        if (!cancelled && controller === request) timer = setTimeout(refresh, 15_000)
      }
    }
    const resume = () => {
      if (document.visibilityState !== 'visible') return
      clearTimeout(timer)
      void refresh()
    }
    void refresh()
    document.addEventListener('visibilitychange', resume)
    return () => {
      cancelled = true
      clearTimeout(timer)
      controller?.abort()
      document.removeEventListener('visibilitychange', resume)
    }
  }, [instanceId, active])

  if (failed && !metrics) return <p className="settings-item-panel__text">Live metrics are unavailable right now.</p>
  const runtime = metrics?.runtime || {}
  const runtimeLine = [runtime.status_label, runtime.region_label, formatDeployedAt(runtime.latest_deployment_at), runtime.data_status].filter(Boolean)
  return (
    <div className="mobius-account__metrics" aria-busy={!metrics || undefined}>
      {metrics
        ? runtimeLine.length > 0 && <p className="mobius-account__runtime">{runtimeLine.join(' · ')}</p>
        : <p className="mobius-account__runtime" role="status"><Spinner size={12} /> Loading live metrics…</p>}
      <div className="mobius-account__meters">
        <MetricMeter label="CPU" value={metrics?.cpu.label} limit={metrics?.cpu.limit_label} percent={metrics?.cpu.percent} />
        <MetricMeter label="Memory" value={metrics?.memory.label} limit={metrics?.memory.limit_label} percent={metrics?.memory.percent} />
        <MetricMeter label="Storage" value={metrics?.volume.used_label} limit={metrics?.volume.allocated_label} percent={metrics?.volume.percent} />
        <MetricMeter
          label="Network"
          value={metrics?.network.rx_label || metrics?.network.tx_label ? `↓ ${metrics.network.rx_label || '0'} · ↑ ${metrics.network.tx_label || '0'}` : ''}
          percent={metrics?.network.percent}
        />
      </div>
      {metrics?.cost && (
        <div className="mobius-account__cost">
          <MetricMeter label={metrics.cost.heading} value="" percent={metrics.cost.percent} />
          {metrics.cost.note && <p className="settings-item-panel__text">{metrics.cost.note}</p>}
        </div>
      )}
    </div>
  )
}

function RenameForm({ name, onSave, onClose }) {
  const [value, setValue] = useState(name)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef(null)
  useEffect(() => { inputRef.current?.focus() }, [])
  const next = value.trim()
  const submit = async event => {
    event.preventDefault()
    if (!next || next === name || pending) return
    setPending(true)
    setError('')
    try {
      await onSave(next)
      onClose()
    } catch (saveError) {
      setError(saveError.message)
      setPending(false)
    }
  }
  return (
    <form className="mobius-account__rename" onSubmit={submit} aria-busy={pending}>
      <input
        ref={inputRef}
        className="mobius-account__input"
        value={value}
        maxLength={80}
        autoComplete="off"
        spellCheck={false}
        aria-label="Deployment name"
        disabled={pending}
        onChange={event => { setValue(event.target.value); setError('') }}
        onKeyDown={event => { if (event.key === 'Escape' && !pending) { event.stopPropagation(); onClose() } }}
      />
      <div className="settings-actions">
        <button type="submit" className="settings__btn settings__btn--sm" disabled={pending || !next || next === name}>{pending ? 'Saving…' : 'Save'}</button>
        <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" disabled={pending} onClick={onClose}>Cancel</button>
      </div>
      <ErrorMessage error={error} />
    </form>
  )
}

function DeploymentCard({ card, dashboard, selfHosted, active, canManage, onRename }) {
  const [renaming, setRenaming] = useState(false)
  const instance = card.railway
  const name = instance?.name || card.name
  const state = deploymentPresentation(instance || card)
  const building = deploymentIsBuilding(instance)
  const canOpen = deploymentCanOpen(instance || card)
  const failed = ['error', 'delete_failed'].includes(String(instance?.status || '').toLowerCase())
  const ready = String(instance?.status || '').toLowerCase() === 'ready'
  const meta = [card.region, card.current && selfHosted && !instance ? 'Self-hosted' : ''].filter(Boolean).join(' · ')
  const openable = card.url && !card.current && canOpen
  const hasActions = instance && (openable || instance.railway_url || deploymentCanRecover(instance) || (instance.actions.retry && failed) || instance.actions.delete || (ready && canManage))
  const hasPanel = (state.detail && state.detail !== state.label) || hasActions || ready || failed || renaming

  return (
    <div className="mobius-account__deployment">
      <div className="settings-item">
        <span className="settings-item__icon mobius-account__deploy-mark"><img src="/moebius.png" alt="" /></span>
        <span className="settings-item__body">
          <span className="settings-item__title">{name}</span>
          <span className="settings-item__sub">
            {/* The local card is already named "This Möbius"; the marker matters when it carries its deployment name. */}
            {card.current && name !== 'This Möbius' ? <span className="mobius-account__here">This Möbius</span> : null}
            {card.current && name !== 'This Möbius' && meta ? ' · ' : ''}
            {meta}
          </span>
        </span>
        <span className="settings-item__trail">
          <span className={`settings-badge ${TONE_BADGE[state.tone] || ''}`}>
            {state.tone === 'progress' && <Spinner size={11} />}
            {state.label}
          </span>
          {!instance && openable && (
            <a className="mobius-account__icon-btn" href={card.url} target="_blank" rel="noopener noreferrer" aria-label={`Open ${name} in a new tab`}>
              <ArrowUpRight width={16} height={16} aria-hidden="true" />
            </a>
          )}
        </span>
      </div>
      {hasPanel && (
        <div className="settings-item-panel mobius-account__deploy-panel">
          {state.detail && state.detail !== state.label && (
            <p className={`settings-item-panel__text${state.tone === 'danger' ? ' mobius-account__danger-text' : ''}`}>{state.detail}</p>
          )}
          {ready && <DeploymentMetrics instanceId={instance.id} active={active} />}
          {ready && (
            <div className="mobius-account__resources">
              <span><strong>Resources</strong> {resourceSummary(instance.resources)}</span>
              {instance.actions.edit_resources && <DashboardLink url={dashboard} label="Change on mobius.you" ariaLabel={`Change resources for ${name}`} />}
            </div>
          )}
          {renaming && <RenameForm name={name} onSave={nextName => onRename(instance.id, nextName)} onClose={() => setRenaming(false)} />}
          {hasActions && !renaming && (
            <div className="settings-actions mobius-account__deploy-actions">
              {openable && (
                <a className="settings__btn settings__btn--sm mobius-account__btn" href={card.url} target="_blank" rel="noopener noreferrer" aria-label={`Open ${name} in a new tab`}>
                  Open<ExternalLink width={13} height={13} aria-hidden="true" />
                </a>
              )}
              {ready && canManage && (
                <button type="button" className="settings__btn settings__btn--outline settings__btn--sm mobius-account__btn" disabled={deploymentNeedsTracking(instance)} onClick={() => setRenaming(true)} aria-label={`Rename ${name}`}>
                  <Pencil width={13} height={13} aria-hidden="true" />Rename
                </button>
              )}
              {instance.railway_url && (instance.status === 'deleting'
                ? <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" disabled>Railway</button>
                : (
                  <a className="settings__btn settings__btn--outline settings__btn--sm mobius-account__btn" href={instance.railway_url} target="_blank" rel="noopener noreferrer" aria-label={`Open ${name} on Railway (opens in a new tab)`}>
                    Railway<ArrowUpRight width={13} height={13} aria-hidden="true" />
                  </a>
                ))}
              {deploymentCanRecover(instance) && <DashboardLink url={dashboard} label="Recover" ariaLabel={`Recover ${name}`} />}
              {instance.actions.retry && failed && (
                <DashboardLink url={dashboard} label={instance.status === 'delete_failed' ? 'Try delete again' : 'Try again'} ariaLabel={`Retry ${name}`} />
              )}
              {instance.actions.delete && (
                <DashboardLink url={dashboard} danger label={building ? 'Cancel deployment' : 'Delete'} ariaLabel={`${building ? 'Cancel deployment' : 'Delete'} ${name}`} />
              )}
            </div>
          )}
          {failed && (
            <div className="mobius-account__resources">
              <span><strong>Project already deleted in Railway?</strong> Remove only its dashboard card after checking. A hidden project may still be running and billed.</span>
              <DashboardLink url={dashboard} label="Remove from dashboard" ariaLabel={`Remove ${name} from the dashboard`} />
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* Railway connect / change-account through the provider's own consent popup.
   The popup is opened inside the click; Möbius then polls the inventory until
   the connection (or a different account) is confirmed. */
function useRailwayConnect(onRailway) {
  const [connecting, setConnecting] = useState(false)
  const [error, setError] = useState('')
  const abortRef = useRef(null)
  useEffect(() => () => abortRef.current?.abort(), [])

  const connect = useCallback((replace, previousAccount) => {
    if (abortRef.current) return
    const popup = window.open('about:blank', 'mobius-railway-connect', 'width=560,height=760')
    if (!popup) {
      setError('Your browser blocked the Railway window. Allow pop-ups, then try again.')
      return
    }
    const controller = new AbortController()
    abortRef.current = controller
    setConnecting(true)
    setError('')
    ;(async () => {
      try {
        const started = await mobiusAccessRequest('/api/identity/railway/connect/start', {
          method: 'POST', signal: controller.signal, ...(replace ? json({ replace: true }) : {}),
        })
        let authorization = null
        try { authorization = new URL(started?.authorization_url) } catch { /* invalid below */ }
        if (authorization?.protocol !== 'https:') throw new Error('Möbius returned an invalid Railway sign-in address.')
        popup.location.replace(authorization.href)
        const deadline = Date.now() + 10 * 60 * 1000
        while (!controller.signal.aborted && Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 900))
          if (controller.signal.aborted) return
          let next = null
          try {
            next = await mobiusAccessRequest('/api/identity/railway?dashboard=1', { signal: controller.signal })
            onRailway(next)
          } catch { /* keep waiting; the popup decides */ }
          if (replace ? railwayAccountChanged(previousAccount, next) : next?.connection?.connected) return
          if (popup.closed) {
            throw new Error(replace && next?.connection?.connected
              ? 'Railway returned the same account. Sign out of Railway in your browser, then try changing the account again.'
              : 'Railway connection was cancelled. Try again when you are ready.')
          }
        }
        if (!controller.signal.aborted) throw new Error('Railway connection took too long. Please try again.')
      } catch (connectError) {
        if (!controller.signal.aborted) setError(connectError.message)
      } finally {
        try { popup.close() } catch { /* already closed */ }
        if (abortRef.current === controller) abortRef.current = null
        if (!controller.signal.aborted) setConnecting(false)
      }
    })()
  }, [onRailway])

  return { connecting, error, connect }
}

function RailwayRows({ railway, connect, onReconnectAccount }) {
  const [open, setOpen] = useState(false)
  const access = railway?.railway_access
  if (access === 'reconnect' && !onReconnectAccount) {
    // A managed deployment's link is not the owner's to replace here.
    return (
      <div className="settings-item">
        <span className="settings-item__body">
          <span className="settings-item__title">Approve deployment controls</span>
          <span className="settings-item__sub settings-item__sub--wrap">Approve the new deployment permission on your mobius.you dashboard to see your Railway deployments here.</span>
        </span>
        <span className="settings-item__trail"><DashboardLink url={railway.dashboard_url} label="Dashboard" ariaLabel="Open your dashboard" /></span>
      </div>
    )
  }
  if (access === 'reconnect') {
    return (
      <div className="settings-item">
        <span className="settings-item__body">
          <span className="settings-item__title">Approve deployment controls</span>
          <span className="settings-item__sub settings-item__sub--wrap">Sign in again once to let this Möbius show and rename your Railway deployments.</span>
        </span>
        <span className="settings-item__trail">
          <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={onReconnectAccount}>Reconnect…</button>
        </span>
      </div>
    )
  }
  if (access !== 'available') return null
  const connected = railway.connection?.connected
  if (!connected) {
    return (
      <div className="settings-item">
        <span className="settings-item__body">
          <span className="settings-item__title">{railway.connection ? 'Reconnect Railway' : 'Connect Railway'}</span>
          <span className="settings-item__sub settings-item__sub--wrap">
            {railway.connection
              ? 'Railway needs you to approve access again before deployments show here.'
              : 'Connect Railway to see your hosted deployments here. Changes still happen on mobius.you.'}
          </span>
        </span>
        <span className="settings-item__trail">
          <button type="button" className="settings__btn settings__btn--sm mobius-account__btn" disabled={connect.connecting} onClick={() => connect.connect(false)}>
            {connect.connecting ? <><Spinner /> Connecting…</> : railway.connection ? 'Reconnect' : 'Connect'}
          </button>
        </span>
      </div>
    )
  }
  return (
    <>
      <button type="button" className="settings-item" aria-expanded={open} aria-controls="mobius-railway-account" onClick={() => setOpen(value => !value)}>
        <span className="settings-item__body">
          <span className="settings-item__title">Railway</span>
          <span className="settings-item__sub">{railway.connection.account || 'Connected'}</span>
        </span>
        <span className="settings-item__trail"><span className="settings-badge settings-badge--success">Connected</span></span>
        <ChevronDown className="settings-item__chevron" width={16} height={16} aria-hidden="true" />
      </button>
      {open && (
        <div className="settings-item-panel" id="mobius-railway-account">
          <p className="settings-item-panel__text">Disconnecting Railway needs your sign-in on the account dashboard.</p>
          <div className="settings-actions">
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm mobius-account__btn" disabled={connect.connecting} onClick={() => connect.connect(true, railway.connection.account)}>
              {connect.connecting ? <><Spinner /> Connecting…</> : 'Change account'}
            </button>
            <a className="settings__btn settings__btn--outline settings__btn--sm mobius-account__btn" href="https://railway.com/workspace/plans" target="_blank" rel="noopener noreferrer" aria-label="Manage your Railway plan (opens in a new tab)">
              Railway plan<ArrowUpRight width={13} height={13} aria-hidden="true" />
            </a>
            <DashboardLink url={railway.dashboard_url} danger label="Disconnect" ariaLabel="Disconnect Railway" />
          </div>
        </div>
      )}
    </>
  )
}

function DeploymentsSection({ identity, signedIn, active, railwayQuery, onReconnectAccount }) {
  const queryClient = useQueryClient()
  const usable = signedIn && !identity.account_unavailable
  const railway = usable ? railwayQuery.data : null
  const setRailway = useCallback(next => queryClient.setQueryData(mobiusAccountQueries.railway.key, next), [queryClient])
  const connect = useRailwayConnect(setRailway)
  const cards = mergeDeployments(identity.deployments, railway?.railway_access === 'available' ? railway.instances : [])
  const tracking = (railway?.instances || []).some(deploymentNeedsTracking)
  const dashboard = railway?.dashboard_url || ''
  const canManage = railway?.railway_access === 'available' && Boolean(railway.connection?.connected)

  const rename = async (id, name) => {
    await mobiusAccessRequest(`/api/identity/railway/deployments/${encodeURIComponent(id)}`, { method: 'PATCH', ...json({ name }) })
    await railwayQuery.refetch()
  }

  return (
    <section className="settings-group" aria-labelledby="mobius-account-deployments">
      <GroupHead
        id="mobius-account-deployments"
        title="Deployments"
        desc={signedIn ? 'Every Möbius on your account. This one comes first.' : 'This Möbius. Sign in to see the others on your account.'}
        aside={tracking ? <span className="mobius-account__live" role="status"><Spinner size={12} /> Updating live</span> : null}
      />
      <div className="settings-list">
        {cards.map(card => (
          <DeploymentCard
            key={card.id}
            card={card}
            dashboard={dashboard}
            selfHosted={identity.account_mode !== 'managed'}
            active={active}
            canManage={canManage}
            onRename={rename}
          />
        ))}
        {usable && railway?.railway_access === 'unavailable' && (
          <div className="settings-item">
            <span className="settings-item__icon"><Warning width={16} height={16} aria-hidden="true" /></span>
            <span className="settings-item__body">
              <span className="settings-item__title">Deployment details are temporarily unavailable</span>
              <span className="settings-item__sub settings-item__sub--wrap">Your account service didn’t answer. Confirmed deployments stay listed above.</span>
            </span>
            <span className="settings-item__trail">
              <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" disabled={railwayQuery.isFetching} onClick={() => railwayQuery.refetch()}>
                {railwayQuery.isFetching ? 'Checking…' : 'Try again'}
              </button>
            </span>
          </div>
        )}
        {usable && <RailwayRows railway={railway} connect={connect} onReconnectAccount={onReconnectAccount} />}
      </div>
      <ErrorMessage error={connect.error} />
      {usable && railwayQuery.error && !railway && (
        <ErrorMessage error={railwayQuery.error} onRetry={() => railwayQuery.refetch()} />
      )}
      {usable && (dashboard || railway?.railway_access === 'available') && (
        <div className="settings-group__foot mobius-account__handoff">
          <span>Create, resize, recover or delete deployments on your mobius.you dashboard, behind your own sign-in. Möbius updates stay in Settings.{!dashboard ? ` ${DASHBOARD_MISSING}` : ''}</span>
          <DashboardLink url={dashboard} label="New deployment" />
        </div>
      )}
    </section>
  )
}

// ── Account ───────────────────────────────────────────────────────────────

function AccountSection({ identity, intent, onIntent, onDisconnected, dashboard }) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const panelRef = useRef(null)
  const confirming = intent === 'disconnect' || intent === 'reconnect'
  useEffect(() => {
    if (confirming) panelRef.current?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' })
  }, [confirming])

  if (identity.account_mode === 'managed') {
    return (
      <section className="settings-group" aria-labelledby="mobius-account-account">
        <GroupHead id="mobius-account-account" title="Account" />
        <div className="settings-list">
          <div className="settings-item">
            <span className="settings-item__body">
              <span className="settings-item__title">Managed by mobius.you</span>
              <span className="settings-item__sub settings-item__sub--wrap">This Möbius runs on your mobius.you account and stays signed in to it. Manage the deployment itself on your dashboard.</span>
            </span>
            {dashboard && <span className="settings-item__trail"><DashboardLink url={dashboard} label="Dashboard" ariaLabel="Open your dashboard" /></span>}
          </div>
        </div>
      </section>
    )
  }

  const disconnect = async () => {
    if (pending) return
    setPending(true)
    setError('')
    try {
      await mobiusAccessRequest('/api/identity/link', { method: 'DELETE' })
      onDisconnected(intent === 'reconnect')
    } catch (disconnectError) {
      setError(disconnectError.status === 502
        ? 'Möbius couldn’t confirm the sign-out with your account, so nothing was disconnected. Try again later.'
        : disconnectError.message)
    } finally {
      setPending(false)
    }
  }
  const reconnecting = intent === 'reconnect'

  return (
    <section className="settings-group" aria-labelledby="mobius-account-account">
      <GroupHead id="mobius-account-account" title="Account" />
      <div className="settings-list">
        <div className="settings-item">
          <span className="settings-item__body">
            <span className="settings-item__title">Linked to mobius.you</span>
            <span className="settings-item__sub settings-item__sub--wrap">This self-hosted Möbius shows and edits your mobius.you account.</span>
          </span>
          {!confirming && (
            <span className="settings-item__trail">
              <button type="button" className="settings__btn settings__btn--danger settings__btn--sm" onClick={() => onIntent('disconnect')}>Disconnect…</button>
            </span>
          )}
        </div>
        {confirming && (
          <div className="settings-item-panel" ref={panelRef} aria-busy={pending}>
            <p className="settings-item-panel__text">
              {reconnecting
                ? 'Your account and deployments stay intact. This link is replaced so you can sign in again and approve deployment management.'
                : 'Your mobius.you account and its deployments stay intact. This Möbius will stop showing or editing that account until you sign in again.'}
            </p>
            <div className="settings-actions">
              <button type="button" className="settings__btn settings__btn--danger settings__btn--sm" disabled={pending} onClick={disconnect}>
                {pending ? (reconnecting ? 'Preparing…' : 'Disconnecting…') : (reconnecting ? 'Disconnect and sign in again' : 'Disconnect')}
              </button>
              <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" disabled={pending} onClick={() => { setError(''); onIntent(null) }}>Keep connected</button>
            </div>
          </div>
        )}
      </div>
      <ErrorMessage error={error} />
    </section>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────

/**
 * The Möbius account page inside Settings. `active` is true while the page is
 * visible; it refreshes the account on return and pauses live polling when
 * hidden. The parent owns the page header (see useMobiusAccountSummary).
 */
export default function MobiusAccountPage({ active = true, onOpenModels = null }) {
  const queryClient = useQueryClient()
  const identityQuery = mobiusAccountQueries.identity.useQuery()
  const identity = identityQuery.data
  const railwayUsable = (identity?.account_mode === 'linked' || identity?.account_mode === 'managed') && !identity.account_unavailable
  // Deployments still building or deleting are followed live while the page is visible.
  const railwayQuery = mobiusAccountQueries.railway.useQuery({
    enabled: railwayUsable,
    refetchInterval: query => (active && (query.state.data?.instances || []).some(deploymentNeedsTracking) ? 2500 : false),
  })
  // 'disconnect' | 'reconnect' while the Account confirm is open; 'signin-again' after a reconnect unlink.
  const [intent, setIntent] = useState(null)
  const wasActiveRef = useRef(active)

  useEffect(() => {
    if (active && !wasActiveRef.current) mobiusAccountQueries.invalidate(queryClient)
    if (!active && (intent === 'disconnect' || intent === 'reconnect')) setIntent(null)
    wasActiveRef.current = active
  }, [active, intent, queryClient])

  const refreshProviders = useCallback(() => {
    authQueries.provider.statuses.invalidate(queryClient)
    modelQueries.registry.invalidate(queryClient)
  }, [queryClient])

  const setIdentity = useCallback(next => {
    queryClient.setQueryData(mobiusAccountQueries.identity.key, next)
  }, [queryClient])

  const signedIn = useCallback(next => {
    setIntent(null)
    setIdentity(next)
    void queryClient.invalidateQueries({
      queryKey: mobiusAccountQueries.key,
      predicate: query => query.queryKey[1] !== 'account',
    })
    refreshProviders()
  }, [queryClient, refreshProviders, setIdentity])

  const disconnected = useCallback(reconnecting => {
    setIntent(reconnecting ? 'signin-again' : null)
    queryClient.removeQueries({ queryKey: [...mobiusAccountQueries.key, 'railway'] })
    queryClient.removeQueries({ queryKey: [...mobiusAccountQueries.key, 'agent'] })
    void mobiusAccountQueries.invalidate(queryClient)
    refreshProviders()
  }, [queryClient, refreshProviders])

  if (!identity) {
    if (identityQuery.isPending) {
      return (
        <div className="mobius-account" aria-busy="true">
          <span className="mobius-account__sr-only" role="status">Loading your Möbius account…</span>
          <section className="settings-group"><SkeletonList rows={3} /></section>
        </div>
      )
    }
    return (
      <div className="mobius-account">
        <section className="settings-group" aria-labelledby="mobius-account-error">
          <GroupHead id="mobius-account-error" title="We couldn’t check your account" desc="This Möbius is still working. Your account details will appear once it can reach the account service." />
          <ErrorMessage error={identityQuery.error} onRetry={() => identityQuery.refetch()} />
        </section>
        <ModelsLink onOpenModels={onOpenModels} />
      </div>
    )
  }

  const mode = identity.account_mode
  const accountSignedIn = mode === 'linked' || mode === 'managed'
  const editable = accountSignedIn && !identity.account_unavailable && Boolean(identity.profile)
  const dashboard = (railwayUsable && railwayQuery.data?.dashboard_url) || ''

  return (
    <div className="mobius-account">
      {identityQuery.error && (
        <ErrorMessage error={`We couldn’t refresh your account. ${identityQuery.error.message} The last confirmed details are shown.`} onRetry={() => identityQuery.refetch()} />
      )}
      {identity.account_unavailable && (
        <section className="settings-group" aria-labelledby="mobius-account-outage">
          <div className="settings-list">
            <div className="settings-item settings-item--top" role="status">
              <span className="settings-item__icon mobius-account__warning-icon"><Warning width={16} height={16} aria-hidden="true" /></span>
              <span className="settings-item__body">
                <span className="settings-item__title settings-item__title--wrap" id="mobius-account-outage">Your {mode === 'managed' ? 'managed' : 'linked'} account is temporarily unavailable</span>
                <span className="settings-item__sub settings-item__sub--wrap">This Möbius keeps working. Account details and editing are paused until the connection recovers.</span>
              </span>
            </div>
          </div>
        </section>
      )}

      {mode === 'signed_out' && <SignInSection onSignedIn={signedIn} reconnecting={intent === 'signin-again'} />}
      {editable && <ProfileSection identity={identity} onIdentity={setIdentity} />}
      <ModelsLink onOpenModels={onOpenModels} />
      <DeploymentsSection
        identity={identity}
        signedIn={accountSignedIn}
        active={active}
        railwayQuery={railwayQuery}
        onReconnectAccount={mode === 'linked' ? () => setIntent('reconnect') : null}
      />
      {accountSignedIn && (
        <AccountSection
          identity={identity}
          intent={intent}
          onIntent={setIntent}
          onDisconnected={disconnected}
          dashboard={dashboard}
        />
      )}
    </div>
  )
}
