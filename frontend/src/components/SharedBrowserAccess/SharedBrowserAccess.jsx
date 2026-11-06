/* Consent and memory-only session gate for trusted shared-browser access. */
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  clearSharedBrowserSession,
  redeemSharedBrowserInvite,
  finalizeSharedBrowserAccount,
  renewSharedBrowserSession,
  leaveSharedBrowserSession,
  leaveSharedBrowserAccess,
  BASE,
} from '../../api/client.js'
import { watchSharedBrowserEntries } from '../../lib/sharedBrowserInvite.js'
import './SharedBrowserAccess.css'

const Shell = lazy(() => import('../Shell/Shell.jsx'))

function entryStatus(entry) {
  if (entry?.kind === 'invite') return 'invited'
  if (entry?.kind === 'leave') return 'leave-confirm'
  if (entry?.kind === 'invalid') return 'ended'
  return 'checking'
}

export default function SharedBrowserAccess({ initialEntry = null }) {
  const queryClient = useQueryClient()
  const [entry, setEntry] = useState(initialEntry)
  const [grant, setGrant] = useState(null)
  const [status, setStatus] = useState(entryStatus(initialEntry))
  const [leftName, setLeftName] = useState('')
  const [logoutFailed, setLogoutFailed] = useState(false)
  const admissionVersionRef = useRef(0)

  useEffect(() => watchSharedBrowserEntries(window, nextEntry => {
    // A same-document hash navigation must be stripped before any UI update.
    // Never keep the previous grant's token or cached results under new consent.
    admissionVersionRef.current += 1
    clearSharedBrowserSession()
    queryClient.clear()
    setGrant(null)
    setLogoutFailed(false)
    setEntry(nextEntry)
    setStatus(entryStatus(nextEntry))
  }), [queryClient])

  useEffect(() => {
    if (entry?.kind === 'invite' || entry?.kind === 'invalid' || entry?.kind === 'leave') return undefined
    let live = true
    const admissionVersion = admissionVersionRef.current
    const admission = entry?.kind === 'account'
      ? finalizeSharedBrowserAccount(entry.value) : renewSharedBrowserSession()
    admission.then(data => {
      if (!live || admissionVersion !== admissionVersionRef.current) return
      setGrant(data.grant)
      setStatus('active')
    }).catch(() => {
      if (live && admissionVersion === admissionVersionRef.current) setStatus('ended')
    })
    return () => { live = false }
  }, [entry])

  useEffect(() => {
    const ended = () => { setGrant(null); setStatus('ended') }
    window.addEventListener('mobius:shared-browser-auth-ended', ended)
    return () => window.removeEventListener('mobius:shared-browser-auth-ended', ended)
  }, [])

  async function accept() {
    if (entry?.kind !== 'invite' || !entry.value) return
    const admissionVersion = admissionVersionRef.current
    setStatus('working')
    const secret = entry.value
    setEntry({ kind: 'invite', value: '' })
    try {
      const data = await redeemSharedBrowserInvite(secret)
      if (admissionVersion !== admissionVersionRef.current) return
      setGrant(data.grant)
      setStatus('active')
    } catch {
      if (admissionVersion === admissionVersionRef.current) setStatus('ended')
    }
  }

  async function giveUpAccess() {
    if (entry?.kind !== 'leave' || !entry.value) return
    const admissionVersion = admissionVersionRef.current
    const pendingId = entry.value
    setEntry({ kind: 'leave', value: '' })
    setStatus('leaving')
    try {
      const data = await leaveSharedBrowserAccess(pendingId)
      if (admissionVersion !== admissionVersionRef.current) return
      setLeftName(data.name)
      setStatus('left')
    } catch {
      if (admissionVersion === admissionVersionRef.current) setStatus('leave-failed')
    }
  }

  async function leave() {
    setGrant(null)
    setStatus('ended')
    try {
      await leaveSharedBrowserSession()
      setLogoutFailed(false)
    } catch {
      setLogoutFailed(true)
    }
  }

  useEffect(() => {
    if (status !== 'active') document.getElementById('splash')?.remove()
  }, [status])

  if (status === 'active' && grant) {
    return <Suspense fallback={<div className="shared-browser-access" role="status">Opening shared Möbius…</div>}>
      <Shell sharedBrowserAccess={{ grant, onLeave: leave }} onInitialVisualReady={() => document.getElementById('splash')?.remove()} />
    </Suspense>
  }
  const settled = ['invited', 'ended', 'leave-confirm', 'left', 'leave-failed'].includes(status)
  const title = {
    invited: 'Trusted access to this Möbius',
    ended: 'Shared access ended',
    'leave-confirm': 'Leave this shared Möbius?',
    leaving: 'Leaving…',
    left: 'You left this shared Möbius',
    'leave-failed': 'Couldn’t leave',
  }[status] || 'Checking shared access…'
  return <main className="shared-browser-access" data-mobius-visual-state={settled ? 'settled' : undefined}>
    <section className="shared-browser-access__card">
      <h1>{title}</h1>
      {status === 'leave-confirm' && <>
        <p>Your access to this Möbius ends now, in every browser, and anything you started here is stopped. It disappears from your Shared with me list.</p>
        <p>To come back, the owner has to invite you again. Close this tab to keep your access.</p>
        <button type="button" onClick={giveUpAccess}>Leave</button>
      </>}
      {status === 'leaving' && <p role="status">Ending your access…</p>}
      {status === 'left' && <p>{leftName ? `Your access to ${leftName} has ended.` : 'Your access has ended.'} You can close this tab.</p>}
      {status === 'leave-failed' && <p>Your access was not changed. Start again from Leave in Shared with me.</p>}
      {status === 'invited' && <>
        <p>This invitation grants access to the same chats, apps, data, and actions on this Möbius instance. It is not a private workspace or a limited preview.</p>
        <p>Accept only if you trust the person who shared it and are comfortable acting in their instance.</p>
        <p>Accepting this invitation replaces any other shared-access sign-in in this browser. Your owner sign-in is not changed.</p>
        <button type="button" onClick={accept}>Accept shared access</button>
      </>}
      {status === 'working' && <p role="status">Opening shared access…</p>}
      {status === 'ended' && <>
        <p>{logoutFailed
          ? 'Access is closed in this tab, but server sign-out could not be confirmed. Reloading may restore access.'
          : 'This sign-in or session is no longer available. Open the instance again from Shared with me, or request a new invitation.'}</p>
        {logoutFailed && <button type="button" onClick={leave}>Retry server sign-out</button>}
        <a href={`${BASE}/shell/`}>Sign in as the owner</a>
      </>}
    </section>
  </main>
}
