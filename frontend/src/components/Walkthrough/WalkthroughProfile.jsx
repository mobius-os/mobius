/* First-screen profile setup. A hosted or already-linked owner picks a handle
   and avatar right here; a self-hosted owner who has not linked yet signs in
   with Möbius through Möbius · You, which owns the account-link handshake. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch, getAuthHeaders, BASE } from '../../api/client.js'
import { detailToMessage } from '../../lib/errorDetail.js'
import { CheckIcon } from './WalkthroughIcons.jsx'
import { returningHandle } from './returningOwner.js'

const HANDLE_PATTERN = /^[a-z0-9_]{3,30}$/
const AVATAR_TYPES = ['image/jpeg', 'image/png', 'image/webp']
const AVATAR_MAX_BYTES = 5 * 1024 * 1024

function avatarProblem(file) {
  if (!AVATAR_TYPES.includes(file.type)) return 'Choose a JPEG, PNG, or WebP image.'
  if (file.size > AVATAR_MAX_BYTES) return 'Choose an image under 5 MB.'
  return ''
}

/* The avatar image is served by the owner's own backend and needs auth, so it
   is fetched into a blob URL (the same way Möbius · You shows it). */
function useAvatarUrl(avatarKey) {
  const [url, setUrl] = useState(null)
  useEffect(() => {
    if (!avatarKey) { setUrl(null); return undefined }
    const controller = new AbortController()
    let objectUrl = null
    apiFetch('/identity/avatar', { signal: controller.signal, timeoutMs: 15_000 }).then(async response => {
      if (!response.ok) return
      objectUrl = URL.createObjectURL(await response.blob())
      setUrl(objectUrl)
    }).catch(() => { /* Initials stay as the fallback artwork. */ })
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [avatarKey])
  return url
}

/* The owner's profile: { loading, error, identity, avatarUrl, editing, returning, claim, uploadAvatar, signIn, reload }.
   `returning` is the handle of an owner who arrived with one, or null (see returningOwner.js).
   claim and uploadAvatar reject with a user-facing Error message. */
export function useAccountProfile(onSignIn) {
  const [state, setState] = useState({ identity: null, loading: true, error: '' })
  // Set when this owner's first handle is claimed in the guide; `reload` refetches after the sign-in hand-off.
  const [claimedHere, setClaimedHere] = useState(false)
  const [editing, setEditing] = useState(false)
  const [reloads, setReloads] = useState(0)
  const reload = useCallback(() => setReloads(count => count + 1), [])
  useEffect(() => {
    const controller = new AbortController()
    apiFetch('/identity', { signal: controller.signal, timeoutMs: 15_000 }).then(async response => {
      if (!response.ok) throw new Error('Your profile is not available right now.')
      setState({ identity: await response.json(), loading: false, error: '' })
    }).catch(error => {
      if (error.name === 'AbortError') return
      // A refresh that fails keeps the profile the owner already saw; only the first load shows the error.
      setState(current => (current.identity
        ? { ...current, loading: false }
        : { identity: null, loading: false, error: error.message || 'Your profile is not available right now.' }))
    })
    return () => controller.abort()
  }, [reloads])
  const avatarUrl = useAvatarUrl(state.identity?.profile?.avatar_url)
  const setIdentity = identity => setState(current => ({ ...current, identity }))

  async function claim(handle) {
    const response = await apiFetch('/identity/profile', { method: 'PATCH', body: JSON.stringify({ handle }), timeoutMs: 20_000 })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(detailToMessage(data.detail, 'Could not claim that handle.'))
    if (!state.identity?.profile?.handle) setClaimedHere(true)
    setIdentity(data)
  }
  async function uploadAvatar(file) {
    // multipart: the browser must set the Content-Type boundary, which apiFetch would override with JSON.
    const body = new FormData()
    body.append('avatar', file)
    const response = await fetch(`${BASE}/api/identity/avatar`, { method: 'POST', headers: getAuthHeaders(), body })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(detailToMessage(data.detail, 'Could not update your avatar.'))
    setIdentity(data)
  }
  const returning = returningHandle({ handle: state.identity?.profile?.handle, claimedHere, editing })
  return { ...state, avatarUrl, claimedHere, editing, setEditing, returning, claim, uploadAvatar, signIn: onSignIn, reload }
}

function Avatar({ url, name, busy, onPick }) {
  const inputRef = useRef(null)
  return <div className="wt-avatar">
    <span className={`wt-avatar__face${busy ? ' is-busy' : ''}`} aria-hidden="true">
      {url ? <img src={url} alt="" /> : <span>{(name || '?').slice(0, 1).toUpperCase()}</span>}
    </span>
    <button type="button" className="wt-avatar__edit" onClick={() => inputRef.current?.click()} disabled={busy} aria-label={url ? 'Change avatar' : 'Upload an avatar'}>
      <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 8h3l2-3h6l2 3h3v11H4z" /><circle cx="12" cy="13" r="3.5" /></svg>
    </button>
    <input ref={inputRef} type="file" accept={AVATAR_TYPES.join(',')} hidden onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) onPick(file) }} />
  </div>
}

export default function WalkthroughProfile({ identityApp, controller }) {
  const identityAvailable = Boolean(identityApp)
  const { identity, loading, error: loadError, avatarUrl, editing, setEditing, returning } = controller
  const [value, setValue] = useState('')
  const [saving, setSaving] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [signingIn, setSigningIn] = useState(false)
  const [error, setError] = useState('')
  const profile = identity?.profile
  const handle = profile?.handle
  const valid = HANDLE_PATTERN.test(value)
  const signedOut = identity && !profile && identity.account_mode === 'signed_out'
  const canEdit = Boolean(profile) && !identity?.account_unavailable

  async function claim(event) {
    event.preventDefault()
    if (!valid || saving) return
    setSaving(true)
    setError('')
    try {
      await controller.claim(value)
      setEditing(false)
    } catch (err) {
      setError(err.message || 'Could not claim that handle.')
    } finally {
      setSaving(false)
    }
  }

  async function pickAvatar(file) {
    const problem = avatarProblem(file)
    if (problem) { setError(problem); return }
    setError('')
    setUploading(true)
    try {
      await controller.uploadAvatar(file)
    } catch (err) {
      setError(err.message || 'Could not update your avatar.')
    } finally {
      setUploading(false)
    }
  }

  async function signIn() {
    setSigningIn(true)
    try { await controller.signIn() } finally { setSigningIn(false) }
  }

  if (loading) return <div className="wt-profile wt-profile--loading" role="status" aria-label="Checking your profile"><span className="wt-skeleton wt-skeleton--avatar" /><span className="wt-skeleton wt-skeleton--line" /></div>
  if (loadError) return <p className="wt-note" role="alert">{loadError} You can set this up later in Möbius · You.</p>

  if (signedOut) {
    return <div className="wt-profile wt-profile--signin">
      <div className="wt-profile__pitch">
        <span className="wt-avatar__face" aria-hidden="true"><span>?</span></span>
        <div><h3>Claim a handle and add a picture</h3><p>Sign in with Möbius to get started. Both are optional to change later, and your email stays private.</p></div>
      </div>
      <button type="button" className="wt-btn wt-btn--primary wt-btn--wide" onClick={signIn} disabled={!identityAvailable || signingIn}>
        <span className="wt-btn__mark" aria-hidden="true"><i /></span>{signingIn ? 'Signing in…' : 'Sign in with Möbius'}
      </button>
      <p className="wt-profile__fine">{identityAvailable ? 'Opens Möbius · You. Come back here when you are done and your guide will be waiting.' : 'Möbius · You is not available right now. You can do this later.'}</p>
    </div>
  }

  if (!canEdit) return <p className="wt-note" role="status">Your Möbius account can’t be reached right now. You can pick a handle later in Möbius · You.</p>

  const showForm = !handle || editing
  return <>
  <div className={`wt-profile${handle && !editing ? ' is-claimed' : ''}`}>
    {showForm && <h3 className="wt-profile__title">Claim a handle and upload a picture <span>(optional)</span> to get started.</h3>}
    <div className="wt-profile__body">
    <div className="wt-profile__picture">
      <Avatar url={avatarUrl} name={handle || profile.display_name} busy={uploading} onPick={pickAvatar} />
    </div>
    <div className="wt-profile__main">
      {showForm ? <form className="wt-handle" onSubmit={claim}>
        <label htmlFor="wt-handle">{handle ? 'New handle' : 'Choose your handle'}</label>
        <div className={`wt-handle__field${error ? ' has-error' : ''}`}>
          <span aria-hidden="true">@</span>
          <input id="wt-handle" value={value} onChange={event => { setValue(event.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '')); setError('') }} maxLength={30} autoComplete="username" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder="yourname" aria-invalid={Boolean(error)} aria-describedby="wt-handle-hint" />
          {valid && <span className="wt-handle__ok"><CheckIcon size={15} /></span>}
        </div>
        <p id="wt-handle-hint" className="wt-profile__fine">Use 3 to 30 lowercase letters, numbers, or underscores. We’ll tell you if it is taken.</p>
        <div className="wt-profile__actions">
          <button type="submit" className="wt-btn wt-btn--primary" disabled={!valid || saving}>{saving ? 'Claiming…' : 'Claim handle'}</button>
          {handle && <button type="button" className="wt-btn" onClick={() => { setEditing(false); setError('') }}>Cancel</button>}
        </div>
      </form> : <div className="wt-claimed" role="status">
        <span className="wt-claimed__badge" aria-hidden="true"><CheckIcon size={14} /></span>
        <div><strong>@{handle}</strong><span>You’re all set. Your email stays private.</span></div>
        <button type="button" className="wt-link" onClick={() => { setEditing(true); setValue('') }}>Change</button>
      </div>}
      {error && <p className="wt-error" role="alert">{error}</p>}
    </div>
    </div>
  </div>
  {returning && <div className="wt-returning" role="note">
    <strong>Welcome back, @{returning}.</strong>
    <span>Your profile is already set, so you can head straight to connecting your agent, or take a quick look around first.</span>
  </div>}
  </>
}
