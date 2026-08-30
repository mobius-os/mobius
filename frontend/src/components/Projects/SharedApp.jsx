import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import ArrowLeft from 'lucide-react/dist/esm/icons/arrow-left.mjs'
import Copy from 'lucide-react/dist/esm/icons/copy.mjs'
import LogOut from 'lucide-react/dist/esm/icons/log-out.mjs'
import RefreshCw from 'lucide-react/dist/esm/icons/refresh-cw.mjs'
import Trash2 from 'lucide-react/dist/esm/icons/trash-2.mjs'
import Users from 'lucide-react/dist/esm/icons/users.mjs'
import {
  api, BASE, beginEphemeralAuth, clearEphemeralAuthSession, getToken, jsonOrThrow,
  setEphemeralAuthSession,
} from '../../api/client.js'
import { assembleProjectHtmlPreview } from '../../lib/projectPreview.js'
import SharedAppFrame from './SharedAppFrame.jsx'
import './SharedApp.css'


function routeInstanceId() {
  try {
    const prefix = `${BASE}/shared/app/`
    return window.location.pathname.startsWith(prefix)
      ? decodeURIComponent(window.location.pathname.slice(prefix.length).split('/')[0])
      : ''
  } catch { return '' }
}

function sessionKey(instanceId) {
  return `mobius:shared-app-session:${instanceId}`
}

function releaseSplash() {
  const splash = document.getElementById('splash')
  if (!splash) return
  splash.style.pointerEvents = 'none'
  splash.style.opacity = '0'
  window.setTimeout(() => splash.remove(), 400)
}

export default function SharedApp() {
  const initialId = routeInstanceId()
  const [phase, setPhase] = useState('boot')
  const [instanceId, setInstanceId] = useState(initialId)
  const [seed, setSeed] = useState(null)
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState('')
  const [joining, setJoining] = useState(false)
  const [doc, setDoc] = useState('')
  const [docError, setDocError] = useState('')
  const [inviteName, setInviteName] = useState('')
  const [invite, setInvite] = useState(null)
  const [copyState, setCopyState] = useState('')
  const [notice, setNotice] = useState('')
  const [publishing, setPublishing] = useState(false)
  const [confirmMemberId, setConfirmMemberId] = useState(null)

  useEffect(() => {
    if (!initialId) {
      setPhase('invite')
    } else {
      let guestToken = ''
      try { guestToken = sessionStorage.getItem(sessionKey(initialId)) || '' } catch { /* ignore */ }
      if (guestToken) {
        beginEphemeralAuth()
        setEphemeralAuthSession(guestToken, null)
      }
      setPhase(guestToken || getToken() ? 'workspace' : 'expired')
    }
    releaseSplash()
  }, [initialId])

  useEffect(() => {
    const expired = () => {
      if (instanceId) {
        try { sessionStorage.removeItem(sessionKey(instanceId)) } catch { /* ignore */ }
      }
      setPhase('expired')
    }
    window.addEventListener('mobius:ephemeral-auth-expired', expired)
    return () => window.removeEventListener('mobius:ephemeral-auth-expired', expired)
  }, [instanceId])

  const instanceQuery = useQuery({
    queryKey: ['shared-app', instanceId],
    queryFn: async () => jsonOrThrow(await api.sharedApps.detail(instanceId), 'Shared app failed:'),
    enabled: phase === 'workspace' && !!instanceId,
    initialData: seed || undefined,
  })
  const stateQuery = useQuery({
    queryKey: ['shared-app', instanceId, 'state'],
    queryFn: async () => jsonOrThrow(await api.sharedApps.state(instanceId), 'Shared data failed:'),
    enabled: phase === 'workspace' && !!instanceId,
  })
  const peopleQuery = useQuery({
    queryKey: ['shared-app', instanceId, 'people'],
    queryFn: async () => jsonOrThrow(await api.sharedApps.collaboration(instanceId), 'People failed:'),
    enabled: phase === 'workspace' && !!instanceId,
    refetchInterval: 5_000,
  })

  useEffect(() => {
    if (
      !instanceQuery.data?.entry_path
      || !instanceQuery.data?.release_id
      || phase !== 'workspace'
    ) return undefined
    const controller = new AbortController()
    let active = true
    setDocError('')
    ;(async () => {
      try {
        const entry = instanceQuery.data.entry_path
        const releaseId = instanceQuery.data.release_id
        const response = await api.sharedApps.output(
          instanceId, releaseId, entry, { signal: controller.signal },
        )
        if (!response.ok) throw new Error(`The shared build could not be loaded (${response.status}).`)
        const html = await response.text()
        const loadText = async path => {
          const dep = await api.sharedApps.output(
            instanceId, releaseId, path, { signal: controller.signal },
          )
          if (!dep.ok) throw new Error(`dependency ${path} failed (${dep.status})`)
          return dep.text()
        }
        const loadDataUri = async path => {
          const dep = await api.sharedApps.output(
            instanceId, releaseId, path, { signal: controller.signal },
          )
          if (!dep.ok) throw new Error(`asset ${path} failed (${dep.status})`)
          const blob = await dep.blob()
          return await new Promise((resolve, reject) => {
            const reader = new FileReader()
            reader.onload = () => resolve(reader.result)
            reader.onerror = () => reject(reader.error)
            reader.readAsDataURL(blob)
          })
        }
        const assembled = await assembleProjectHtmlPreview(html, entry, loadText, loadDataUri, 'shared')
        if (active) setDoc(assembled)
      } catch (cause) {
        if (active && cause?.name !== 'AbortError') setDocError(cause?.message || 'The shared build could not be loaded.')
      }
    })()
    return () => { active = false; controller.abort() }
  }, [
    instanceId,
    instanceQuery.data?.entry_path,
    instanceQuery.data?.release_id,
    phase,
  ])

  async function join(event) {
    event.preventDefault()
    const secret = window.location.hash.replace(/^#/, '').split('?')[0]
    if (!displayName.trim() || !secret || joining) return
    setJoining(true); setError('')
    try {
      const payload = await jsonOrThrow(await api.sharedApps.redeemInvite({
        invite: secret, display_name: displayName.trim(),
      }), 'Invitation failed:')
      setEphemeralAuthSession(payload.access_token, null)
      try { sessionStorage.setItem(sessionKey(payload.instance.id), payload.access_token) } catch { /* tab remains usable */ }
      setInstanceId(payload.instance.id)
      setSeed(payload.instance)
      window.history.replaceState(null, '', `${BASE}/shared/app/${encodeURIComponent(payload.instance.id)}`)
      setPhase('workspace')
    } catch (cause) {
      setError(cause?.message || 'This invitation could not be accepted.')
    } finally { setJoining(false) }
  }

  async function createInvite() {
    try {
      setError('')
      setCopyState('')
      setInvite(await jsonOrThrow(await api.sharedApps.createInvite(instanceId, {
        invitee_name: inviteName.trim() || null, role: 'editor',
      }), 'Invitation failed:'))
      await peopleQuery.refetch()
    } catch (cause) { setError(cause?.message || 'Could not create an invitation.') }
  }

  async function publishLatest() {
    if (publishing) return
    setPublishing(true); setError(''); setNotice('')
    try {
      await jsonOrThrow(await api.sharedApps.publishRelease(instanceId), 'Publish failed:')
      await instanceQuery.refetch()
      setNotice('Latest build published. Shared data was kept.')
    } catch (cause) {
      setError(cause?.message || 'Could not publish the latest build.')
    } finally { setPublishing(false) }
  }

  async function revokeInvite(inviteId) {
    setError('')
    try {
      const response = await api.sharedApps.revokeInvite(instanceId, inviteId)
      if (!response.ok) throw new Error(`Invitation update failed (${response.status}).`)
      await peopleQuery.refetch()
    } catch (cause) { setError(cause?.message || 'Could not revoke the invitation.') }
  }

  async function changeMemberRole(memberId, role) {
    setError('')
    try {
      await jsonOrThrow(
        await api.sharedApps.updateMember(instanceId, memberId, { role }),
        'Role update failed:',
      )
      await peopleQuery.refetch()
    } catch (cause) { setError(cause?.message || 'Could not update this person.') }
  }

  async function removeMember(memberId) {
    if (confirmMemberId !== memberId) {
      setConfirmMemberId(memberId)
      return
    }
    setError('')
    try {
      const response = await api.sharedApps.removeMember(instanceId, memberId)
      if (!response.ok) throw new Error(`Remove failed (${response.status}).`)
      setConfirmMemberId(null)
      await peopleQuery.refetch()
    } catch (cause) { setError(cause?.message || 'Could not remove this person.') }
  }

  async function copyInvite() {
    if (!invite?.join_url) return
    try { await navigator.clipboard.writeText(invite.join_url); setCopyState('Copied') }
    catch { setCopyState('Select the link') }
  }

  function leave() {
    try { sessionStorage.removeItem(sessionKey(instanceId)) } catch { /* ignore */ }
    clearEphemeralAuthSession()
    setPhase('expired')
  }

  if (phase === 'boot') return <main className="shared-app shared-app--center" aria-busy="true" />
  if (phase === 'invite') return <main className="shared-app shared-app--center" data-mobius-visual-state="settled"><section className="shared-app__join">
    <span className="shared-app__join-mark"><Users size={22} /></span>
    <p>Private app invitation</p><h1>Use this app together</h1>
    <span>You can use the shared app and its live data. Its source project stays private.</span>
    <form onSubmit={join}><label htmlFor="shared-app-name">Your name</label><input id="shared-app-name" autoFocus maxLength="128" value={displayName} onChange={event => setDisplayName(event.target.value)} /><button type="submit" disabled={joining || !displayName.trim()}>{joining ? 'Joining…' : 'Join app'}</button></form>
    {error && <div className="shared-app__error" role="alert">{error}</div>}
  </section></main>
  if (phase === 'expired') return <main className="shared-app shared-app--center" data-mobius-visual-state="settled"><section className="shared-app__join" role="alert"><h1>This app session has ended</h1><span>Ask the app owner for a new invitation.</span></section></main>

  const instance = instanceQuery.data
  if (instanceQuery.isLoading || stateQuery.isLoading) return <main className="shared-app shared-app--center"><p role="status">Opening shared app…</p></main>
  if (!instance || instanceQuery.isError || stateQuery.isError) return <main className="shared-app shared-app--center" data-mobius-visual-state="settled"><section className="shared-app__join" role="alert"><h1>Couldn’t open this app</h1><span>{instanceQuery.error?.message || stateQuery.error?.message}</span></section></main>
  const people = peopleQuery.data?.members || []
  const isOwner = instance.role === 'owner'
  return <main className="shared-app" data-mobius-visual-state="settled">
    <header className="shared-app__header">
      {isOwner && instance.project_id && <button type="button" className="shared-app__icon-button" aria-label="Back to project" onClick={() => window.location.assign(`${BASE}/shell/?project=${encodeURIComponent(instance.project_id)}`)}><ArrowLeft size={19} /></button>}
      <div className="shared-app__identity"><span>Live shared app</span><h1>{instance.name}</h1></div>
      <span className="shared-app__people"><Users size={15} />{people.length || 1}</span>
      {isOwner && <button type="button" className="shared-app__publish" disabled={publishing} onClick={publishLatest}><RefreshCw size={15} />{publishing ? 'Publishing…' : 'Publish latest'}</button>}
      {!isOwner && <button type="button" className="shared-app__icon-button" aria-label="Leave app" onClick={leave}><LogOut size={18} /></button>}
    </header>
    {isOwner && <section className="shared-app__invite" aria-label="Invite someone to this app">
      <input value={inviteName} maxLength="128" placeholder="Collaborator name (optional)" aria-label="Collaborator name" onChange={event => setInviteName(event.target.value)} />
      <button type="button" onClick={createInvite}>Invite</button>
      {invite?.join_url && <div className="shared-app__invite-link"><input readOnly value={invite.join_url} aria-label="App invitation link" onFocus={event => event.currentTarget.select()} /><button type="button" onClick={copyInvite}><Copy size={14} />{copyState || 'Copy'}</button></div>}
      <details className="shared-app__access">
        <summary>Access · {Math.max(0, people.length - 1)} {people.length === 2 ? 'person' : 'people'}</summary>
        {people.filter(person => person.id !== 'owner').map(person => <div key={person.id} className="shared-app__access-row">
          <span>{person.display_name}{person.you ? ' · you' : ''}</span>
          <select value={person.role} aria-label={`Role for ${person.display_name}`} onChange={event => changeMemberRole(person.id, event.target.value)}>
            <option value="editor">Can edit</option><option value="viewer">Can view</option>
          </select>
          <button type="button" className={confirmMemberId === person.id ? 'shared-app__danger' : 'shared-app__quiet'} onClick={() => removeMember(person.id)}><Trash2 size={14} />{confirmMemberId === person.id ? 'Confirm remove' : 'Remove'}</button>
        </div>)}
        {(peopleQuery.data?.invites || []).map(pending => <div key={pending.id} className="shared-app__access-row">
          <span>{pending.invitee_name || 'Invitation'} · pending</span>
          <button type="button" className="shared-app__quiet" onClick={() => revokeInvite(pending.id)}>Revoke</button>
        </div>)}
      </details>
    </section>}
    {notice && <p className="shared-app__notice" role="status">{notice}</p>}
    {error && <p className="shared-app__error" role="alert">{error}</p>}
    <section className="shared-app__surface">
      {docError ? <div className="shared-app__empty" role="alert">{docError}</div> : doc ? <SharedAppFrame instanceId={instanceId} srcDoc={doc} initialState={stateQuery.data} title={`${instance.name} shared app`} /> : <div className="shared-app__empty" role="status">Preparing shared app…</div>}
    </section>
  </main>
}
