import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Branch, ChevronDown, ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import { appQueries } from '../../../hooks/queries.js'
import { sharedBrowserShellHref } from '../../../lib/sharedBrowserWorkspace.js'
import { passiveAppBlockAllowed } from '../../../lib/passiveAppBlocks.js'
import { inlineBlockState, inlineBlockStateUpdate, inlineSessionRetained, inlineBlockDocumentReset } from './appBlock.js'
import useAppBlockCapability from '../hooks/useAppBlockCapability.js'
import './AppBlock.css'

const AppCanvas = lazy(() => import('../../AppCanvas/AppCanvas.jsx'))

const STATE_NAMES = { proposed: 'Not sent yet', open: 'Open', draft: 'Draft', merged: 'Merged', closed: 'Closed' }
// State pills use their own tone, distinct from link styling.
const STATE_TONES = { proposed: 'neutral', open: 'success', draft: 'neutral', merged: 'success', closed: 'danger' }

/** GitHub's label treatment: tinted fill with lifted text in dark mode, the
 *  solid label color with black or white text in light mode. */
function labelStyle(hex) {
  if (!hex) return undefined
  const [r, g, b] = [0, 2, 4].map(at => parseInt(hex.slice(at, at + 2), 16))
  const max = Math.max(r, g, b) / 255, min = Math.min(r, g, b) / 255
  const l = (max + min) / 2, d = max - min
  const s = d ? d / (1 - Math.abs(2 * l - 1)) : 0
  const h = !d ? 0 : max === r / 255 ? 60 * (((g - b) / 255 / d) % 6) : max === g / 255 ? 60 * ((b - r) / 255 / d + 2) : 60 * ((r - g) / 255 / d + 4)
  const perceived = (r * 0.2126 + g * 0.7152 + b * 0.0722) / 255
  const lifted = Math.min(100, l * 100 + Math.max(0, (0.6 - perceived) * 100))
  const hsl = alpha => `hsla(${((h + 360) % 360).toFixed(1)},${(s * 100).toFixed(1)}%,${lifted.toFixed(1)}%,${alpha})`
  return {
    '--md-label-dark-bg': `rgba(${r},${g},${b},0.18)`, '--md-label-dark-fg': hsl(1), '--md-label-dark-border': hsl(0.3),
    '--md-label-light-bg': `rgb(${r},${g},${b})`, '--md-label-light-fg': perceived > 0.453 ? '#1f2328' : '#ffffff',
  }
}

/** A PR as a compact row: the title is the link into the app, details sit
 *  underneath, and the app's one action takes the bottom-right corner. */
export function PullSnapshot({ block, pull, href, open, action, session, compact = false }) {
  const files = pull.files === null ? null : `${pull.files} ${pull.files === 1 ? 'file' : 'files'}`
  const ref = !compact && pull.number ? `${pull.repo}#${pull.number}` : pull.repo
  const status = session?.status || STATE_NAMES[pull.state]
  const statusTone = session?.status ? session.statusTone : STATE_TONES[pull.state]
  const labels = pull.labels.map(label => <span key={label.name} className="md-app-pull__label" style={labelStyle(label.color)}>{label.name}</span>)
  const links = session?.links?.length ? <span className="md-app-block__links">{session.links.map(link => <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer">{link.label}</a>)}</span> : null
  return <div className={`md-app-pull${compact ? ' md-app-pull--compact' : ''}`}>
    {compact ? <span className="md-app-pull__identity-icon" aria-hidden="true"><Branch width={18} height={18} /></span> : null}
    <div className="md-app-pull__headline">
      <a className="md-app-pull__title" href={href} onClick={open}>{block.title}</a>
      {compact ? <span className={`md-app-pull__status is-${statusTone}`}>{status}</span> : labels}
    </div>
    {compact && labels.length ? <div className="md-app-pull__labels">{labels}</div> : null}
    <div className="md-app-pull__footer">
      <div className="md-app-pull__meta">
        <span className="md-app-pull__source">
          <a className="md-app-pull__repo" href={compact ? pull.repoUrl : pull.url || pull.repoUrl} target="_blank" rel="noopener noreferrer">{ref}</a>
          {pull.author ? <span>{pull.author}</span> : null}
        </span>
        <span className="md-app-pull__changes">
          {files ? <span>{files}{pull.additions !== null ? <> <ins>+{pull.additions}</ins> <del>−{pull.deletions ?? 0}</del></> : null}</span> : null}
          {!compact ? <span className={`md-app-pull__badge is-${statusTone}`}>{status}</span> : null}
          {(session?.badges ?? pull.badges).map(badge => <span key={badge.label} className={`md-app-pull__badge is-${badge.tone}`}>{badge.label}</span>)}
        </span>
      </div>
      <div className="md-app-pull__outcome">{links}{action}</div>
    </div>
    {session?.note ? <p className={`md-app-block__note is-${session.tone}`} role="status">{session.note}</p> : null}
  </div>
}

/** A deliberate confirmation step presents the app's complete frozen action,
 *  not the possibly stale or mismatched transcript snapshot above it. */
export function InlineConfirmation({ state, competingBusy, onConfirm, onCancel }) {
  if (!state) return null
  return <div className="md-app-block__confirmation">
    <strong>Confirm current changes</strong>
    {state.confirmation ? <ul>{state.confirmation.map((item, i) => <li key={i}>
      <span>{item.title}</span>
      <dl>{item.facts.map((fact, j) => <div key={j}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}</dl>
    </li>)}</ul> : <p role="status">{state.note}</p>}
    <div className="md-app-block__controls">
      <button type="button" className="md-app-block__cancel" disabled={competingBusy} onClick={onCancel}>Not now</button>
      <button type="button" className="md-app-block__action" disabled={state.disabled || competingBusy}
        onClick={onConfirm}>Confirm</button>
    </div>
  </div>
}

/** Reuse the opaque app host; idle sessions exist only near the viewport. */
export default function AppBlock({ block, onInternalNav }) {
  const apps = appQueries.list.useQuery()
  const app = (apps.data || []).find(item => item.slug === block.app && !item.deleted_at)
  const canExpand = block.inline !== false
  const isSession = block.interaction === 'inline'
  const passiveAllowed = passiveAppBlockAllowed(app)
  const [legacyMode, setLegacyMode] = useState(null)
  const rootRef = useRef(null)
  const [nearViewport, setNearViewport] = useState(false)
  const [sessionId] = useState(() => crypto.randomUUID())
  const [blockEvent, setBlockEvent] = useState(null)
  const blockEventRef = useRef(null)
  blockEventRef.current = blockEvent
  const [sessionState, setSessionState] = useState(null)
  const [viewIntent, setViewIntent] = useState(null)
  const retained = inlineSessionRetained(sessionState, blockEvent)
  const viewIntentRef = useRef(viewIntent)
  viewIntentRef.current = viewIntent
  const negotiating = !isSession && canExpand && viewIntent !== null && passiveAllowed
  // Scalar handover dependencies prevent a state/init echo loop: the parser
  // copies envelopes on every message even when their observational data agrees.
  const checkpointId = sessionState?.checkpoint?.id ?? null
  const checkpointData = sessionState?.checkpoint?.data ?? null
  const recoveryError = sessionState?.recoveryError ?? null
  const blockSession = useMemo(() => (isSession && (passiveAllowed || retained) || negotiating) ? {
    sessionId, retain: retained, checkpoint: checkpointId ? { id: checkpointId, data: checkpointData } : null,
    recoveryError,
    actions: [block.action, ...block.items.map(item => item.action)].filter(Boolean)
      .map(action => ({ key: action.intent, intent: action.intent, label: action.label })),
  } : null, [isSession, passiveAllowed, negotiating, sessionId, retained, checkpointId, checkpointData, recoveryError, block])
  const allowedKeys = useMemo(() => new Set(blockSession?.actions.map(action => action.key) || []), [blockSession])
  const onSessionFallback = useCallback(key => {
    if (retained) return // unsupported replacement cannot erase unresolved ownership
    setLegacyMode('view'); setViewIntent(key); setBlockEvent(null); setSessionState(null)
  }, [retained])
  const { supported: blockSupported, remember: rememberBlockEvent, observe: observeBlockCapability } =
    useAppBlockCapability({ allowedKeys, onFallback: onSessionFallback })
  useEffect(() => {
    if (!isSession || !rootRef.current || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(entries => {
      setNearViewport(entries.some(entry => entry.isIntersecting))
    }, { rootMargin: '400px 0px' })
    observer.observe(rootRef.current)
    return () => observer.disconnect()
  }, [isSession])
  const onBlockState = useCallback(message => {
    const safe = inlineBlockState(message, sessionId, allowedKeys)
    if (safe) {
      setSessionState(previous => inlineBlockStateUpdate(previous, safe))
      if (!safe.checkpointInvalid) setBlockEvent(event => safe.ackNonce === event?.nonce ? null : event)
    }
  }, [sessionId, allowedKeys])
  const actionState = key => sessionState?.actions.find(item => item.key === key)
  const competingBusy = sessionState?.actions.some(item => item.busy)
  const dispatchBlockEvent = useCallback((key, event) => {
    const message = { sessionId, key, event, nonce: crypto.randomUUID() }
    rememberBlockEvent(message)
    setBlockEvent(message)
  }, [sessionId, rememberBlockEvent])
  const onBlockCapability = useCallback((supported, document) => {
    // A real document change invalidates idle UI, never an unresolved owner.
    // Active publication prevents promotion; a reload still needs observation.
    if (document?.reset) {
      setSessionState(previous => inlineBlockDocumentReset(previous, blockEventRef.current))
      setBlockEvent(null) // old-document Confirm nonce cannot be replayed
    }
    if (isSession) {
      observeBlockCapability(supported)
      return
    }
    const intent = viewIntentRef.current
    if (!intent) return
    if (supported && allowedKeys.has(intent)) {
      setLegacyMode('inline')
      dispatchBlockEvent(intent, 'activate')
    } else setLegacyMode('view')
  }, [isSession, allowedKeys, dispatchBlockEvent, observeBlockCapability])
  // The open view's intent: the block's own, or its action's. null = closed.
  const [delivered, setDelivered] = useState(false)
  const pending = useMemo(() => viewIntent ? { intent: viewIntent, nonce: crypto.randomUUID() } : null, [viewIntent])
  const href = sharedBrowserShellHref(block.href)
  const navigate = useCallback((target) => {
    const url = new URL(sharedBrowserShellHref(target), window.location.href)
    if (onInternalNav) onInternalNav(url)
    else window.location.assign(url.href)
  }, [onInternalNav])
  // A transcript is not a workspace navigation owner or a second chat-control
  // authority, so the embedded view may only open a conversation or an app.
  const hostRequest = useCallback((_, request) => {
    if (request.type === 'moebius:open-chat' && request.chatId) {
      navigate(`/shell/?${new URLSearchParams({ chat: request.chatId })}`)
    } else if (request.type === 'moebius:open-app' && request.appId) {
      navigate(`/shell/?${new URLSearchParams({ app: request.appId, ...(request.intent ? { intent: request.intent } : {}) })}`)
    } else {
      throw new Error('Open the app to use workspace controls. This transcript view can only open conversations and apps.')
    }
  }, [navigate])
  const openHref = target => event => {
    if (!onInternalNav || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault(); navigate(target)
  }
  const open = openHref(block.href)
  const show = intent => { setViewIntent(intent); setLegacyMode(!passiveAllowed || isSession && blockSupported === false ? 'view' : null); setDelivered(false); setSessionState(null); setBlockEvent(null) }
  const actionButton = target => (isSession && (passiveAllowed || retained) && (blockSupported !== false || retained) || legacyMode === 'inline') && app && target
    ? (() => {
      const state = actionState(target.intent)
      // Saved actions are history, not current controls. Wait for the app's
      // exact read before showing either a button or its settled link.
      if (!state) return <span className="md-app-block__pending" role="status">Loading…</span>
      if (state.hidden) return null
      if (state.confirming) return <span className="md-app-block__pending">Review below</span>
      return <span className="md-app-block__controls">
        <button type="button" className="md-app-block__action" disabled={state?.disabled || competingBusy}
          title={state?.label || target.label}
          aria-busy={state?.busy || undefined}
          onClick={() => dispatchBlockEvent(target.intent, 'activate')}>
          {state.label || target.label}</button>
      </span>
    })()
    : canExpand && app && target
    ? <button type="button" className="md-app-block__action" aria-pressed={viewIntent === target.intent}
      onClick={() => show(viewIntent === target.intent ? null : target.intent)}>{target.label}</button>
    : null
  const action = actionButton(block.action)
  const confirming = sessionState?.actions.find(item => item.confirming)
  const confirmation = <InlineConfirmation state={confirming} competingBusy={competingBusy}
    onCancel={() => dispatchBlockEvent(confirming.key, 'cancel')}
    onConfirm={() => dispatchBlockEvent(confirming.key, 'confirm')} />
  const toggle = canExpand && app && !isSession && legacyMode !== 'inline'
    ? <button type="button" className="md-app-block__toggle" aria-expanded={viewIntent !== null}
      onClick={() => show(viewIntent === null ? block.intent : null)}>
      <ChevronDown width={16} height={16} aria-hidden="true" />{viewIntent !== null ? 'Hide details' : block.expandLabel || 'Show details here'}</button>
    : null
  const view = isSession && (passiveAllowed || retained) && (blockSupported !== false || retained) && app && (nearViewport || retained) ? <div className="md-app-block__session-host" aria-hidden="true" inert="">
    <Suspense fallback={null}><AppCanvas appId={app.id} appName={app.name} appSlug={app.slug}
      version={app.updated_at || 0} offlineCapable={app.offline_capable} capabilityContract={app.capability_contract || app.capabilities}
      active={false} visible={false} interactive={false} blockSession={blockSession} blockEvent={blockEvent}
      onBlockState={onBlockState} onBlockCapability={onBlockCapability} onHostRequest={hostRequest} /></Suspense>
  </div> : canExpand && app && pending && (!isSession || !passiveAllowed || blockSupported === false) && <div className={legacyMode === 'view' ? 'md-app-block__view' : 'md-app-block__session-host'}
    style={legacyMode === 'view' ? { height: block.height } : undefined} aria-hidden={legacyMode !== 'view' ? 'true' : undefined} inert={legacyMode !== 'view' ? '' : undefined}>
    <Suspense fallback={legacyMode === 'view' ? <p role="status">Opening details…</p> : null}><AppCanvas key={viewIntent} appId={app.id} appName={app.name} appSlug={app.slug}
      version={app.updated_at || 0} offlineCapable={app.offline_capable} capabilityContract={app.capability_contract || app.capabilities}
      active={false} visible={legacyMode === 'view'} interactive={legacyMode === 'view'} pendingIntent={legacyMode === 'view' && !delivered ? pending : null}
      blockSession={legacyMode === 'view' ? null : blockSession} blockEvent={legacyMode === 'view' ? null : blockEvent} onBlockState={onBlockState} onBlockCapability={onBlockCapability}
      onIntentDelivered={() => setDelivered(true)} onHostRequest={hostRequest} /></Suspense>
  </div>
  const unavailable = canExpand && !app
    ? <p>{apps.isLoading ? 'Checking installed apps…' : `${block.app} is not available. The saved snapshot remains here.`}</p>
    : null
  if (block.items.length > 0) {
    return <section ref={rootRef} className={`md-app-block md-app-block--batch${isSession ? ' md-app-block--compact' : ''}`} aria-label={sessionState?.summary || block.title}>
      <header className="md-app-batch__head"><strong>{sessionState?.summary || block.title}</strong><span>{block.items.length} {block.items.length === 1 ? 'item' : 'items'}</span></header>
      <ul className="md-app-batch__list">
        {block.items.map(item => <li key={item.intent}>
          {item.pull
            ? <PullSnapshot block={item} pull={item.pull} href={sharedBrowserShellHref(item.href)} open={openHref(item.href)} action={actionButton(item.action)} session={actionState(item.action?.intent)} compact={isSession} />
            : <a className="md-app-batch__title" href={sharedBrowserShellHref(item.href)} onClick={openHref(item.href)}>{item.title}</a>}
        </li>)}
      </ul>
      {(action || isSession) ? <footer className="md-app-batch__foot">
        {actionState(block.action?.intent)?.note ? <p className={`md-app-block__note is-${actionState(block.action.intent).tone}`} role="status">{actionState(block.action.intent).note}</p> : null}
        {action}
      </footer> : null}
      {confirmation}
      {sessionState?.notice ? <p className="md-app-block__notice" role="status">{sessionState.notice}</p> : null}
      {view}
    </section>
  }
  if (block.pull) {
    // The title already opens the app, so a PR row has no separate Open link
    // or details toggle: its one action is the only inline view.
    const label = block.pull.number ? `Pull request ${block.pull.repo}#${block.pull.number}` : `Proposed pull request for ${block.pull.repo}`
    return <section ref={rootRef} className={`md-app-block md-app-block--pull${isSession ? ' md-app-block--compact' : ''}`} aria-label={label}>
      <PullSnapshot block={block} pull={block.pull} href={href} open={open} action={action} session={actionState(block.action?.intent)} compact={isSession} />
      {confirmation}
      {sessionState?.notice ? <p className="md-app-block__notice" role="status">{sessionState.notice}</p> : null}
      {view}
    </section>
  }
  return <section ref={rootRef} className={`md-app-block${canExpand ? '' : ' md-app-block--link'}`} aria-label={block.title}>
    <header><strong>{block.title}</strong><span className="md-app-block__header-actions">
      {actionState(block.action?.intent)?.links.length ? <span className="md-app-block__links">{actionState(block.action.intent).links.map(link =>
        <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer">{link.label}</a>)}</span> : null}
      {action}<a href={href} onClick={open}>Open{app ? ` in ${app.name}` : ''}<ChevronRight width={16} height={16} aria-hidden="true" /></a></span></header>
    {block.facts.length > 0 && <dl>{block.facts.map((fact, i) => <div key={i}><dt>{fact.label}</dt><dd>{fact.href ? <a href={fact.href} target="_blank" rel="noopener noreferrer">{fact.value}</a> : fact.value}</dd></div>)}</dl>}
    {toggle}{unavailable}{confirmation}{sessionState?.notice ? <p className="md-app-block__notice" role="status">{sessionState.notice}</p> : null}{view}
  </section>
}
