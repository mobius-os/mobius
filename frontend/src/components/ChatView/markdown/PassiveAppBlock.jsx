/* Passive block session: a hidden, inert instance of an admitted app reports
   current state while the block is near the viewport; the host draws every
   control. See building-apps.md "Passive block sessions". */
import { Suspense, useCallback, useEffect, useId, useState } from 'react'
import { Chat } from '@openai/apps-sdk-ui/components/Icon'
import { appFrameVersion } from '../../../lib/appVersion.js'
import useAppBlockSession from '../hooks/useAppBlockSession.js'
import { ASK_LABEL, askMessage, passiveAskAllowed, sessionSnapshot } from './appBlockSession.js'
import { AppBlockLayout, AppCanvas, AppLink, ExternalLinks, StaticAppBlock, StatusPill } from './AppBlockParts.jsx'
import { usePassiveSlot } from './passiveSlots.js'
import { useNearViewport } from './blockViewport.js'
import { passiveBlockMode } from './blockOwnership.js'
import { appBlockChatKey, blockFingerprint, useAppBlockChat } from './appBlockChat.js'

export { passiveBlockMode }

/** Whether an element is actually on screen in a visible document (no
 *  prefetch margin), as `{ showing, visible }`. The observation belongs to the
 *  showing it was made for: on a new showing it starts unseen and a fresh
 *  observer reports the element's position after that showing's layout, so a
 *  previous showing's intersection is never borrowed. */
export function useOnScreen(showing) {
  const [node, setNode] = useState(null)
  const [seen, setSeen] = useState({ showing: null, intersecting: false })
  const [documentVisible, setDocumentVisible] = useState(() =>
    typeof document === 'undefined' || document.visibilityState === 'visible')
  useEffect(() => {
    setSeen({ showing, intersecting: false })
    if (!node || !showing || typeof IntersectionObserver === 'undefined') return undefined
    const observer = new IntersectionObserver(entries => setSeen({ showing, intersecting: entries.at(-1).isIntersecting }))
    observer.observe(node)
    return () => observer.disconnect()
  }, [node, showing])
  useEffect(() => {
    const update = () => setDocumentVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', update)
    return () => document.removeEventListener('visibilitychange', update)
  }, [])
  return [setNode, { showing: seen.showing, visible: seen.intersecting && documentVisible }]
}

/** No new owner event while one awaits acknowledgement, an action is busy,
 *  or admission was withdrawn under an unresolved session. */
export function sessionLocked(session, admitted) {
  return !admitted || session.awaitingAck || Boolean(session.state?.actions.some(action => action.busy))
}


/** Host-drawn controls for one app action. Before this document reports,
 *  saved actions are history, not controls. Confirm takes the action's own
 *  fixed-size place; "Not now" sits beside it. An `open` action is a plain
 *  link to the block in its app (`openHref`) and sends no event. */
export function SessionControls({ target, state, locked, confirmArmed = false, confirmRef = null, onEvent,
  openHref = '', onInternalNav = null }) {
  const waitingId = useId()
  if (!state) return <span className="md-app-block__pending" role="status">Loading…</span>
  if (state.hidden) return null
  // Only assistive technology hears why an otherwise ready Confirm waits.
  const waiting = state.confirming && Boolean(state.confirmation) && !confirmArmed && !locked
  const control = state.confirming
    ? <span className="md-app-block__controls">
      <button type="button" className="md-app-block__cancel" disabled={locked}
        onClick={() => onEvent(target.intent, 'cancel')}>Not now</button>
      <button ref={confirmRef} type="button" className="md-app-block__action"
        disabled={locked || state.disabled || !state.confirmation || !confirmArmed}
        aria-describedby={waiting ? waitingId : undefined}
        onClick={() => onEvent(target.intent, 'confirm')}>Confirm</button>
      {waiting ? <span id={waitingId} className="md-app-block__sr-only">
        Confirm turns on once the first and last rows above have been on screen.</span> : null}
    </span>
    : state.open && openHref
      ? <AppLink className="md-app-block__action is-secondary" href={openHref} onInternalNav={onInternalNav}>
        {state.label || target.label}</AppLink>
      : <button type="button" className="md-app-block__action" disabled={locked || state.disabled}
        aria-busy={state.busy || undefined} title={state.label || target.label}
        onClick={() => onEvent(target.intent, 'activate')}>
        {state.busy ? <span className="md-app-block__spinner" aria-hidden="true" /> : null}
        {state.label || target.label}</button>
  return <div className="md-app-block__session">
    <div className="md-app-block__session-row">
      <StatusPill status={state.status} tone={state.statusTone} />
      <ExternalLinks links={state.links} />
      {control}
    </div>
    {/* A confirmation's own rows replace the note; one that cannot be shown
        (incomplete or ambiguous) says why here. */}
    {state.note && !state.confirmation ? <p className={`md-app-block__note is-${state.tone}`} role="status">{state.note}</p> : null}
  </div>
}

/** The ask this block may draw now, or null: only one its admitted app
 *  reported in this document (never a saved snapshot), on the newest block of
 *  its kind in the chat the transcript attests, outside recovery. The parser
 *  already drops an ask while an action confirms or is busy. */
export function blockAsk({ state, admitted, allowed, context }) {
  return state?.ask && admitted && allowed && Boolean(context?.chatId) && context.latest === true && !state.recoveryPending
    ? state.ask : null
}

const ASK_LABELS = { sending: 'Asking…', asked: 'Asked' }
const ASK_WAIT = { sending: 'Sending a request to the agent in this chat.', asked: 'The request is in this chat.' }

/** The host's ask control (`ask-v1`): its label is the host's, and one tap
 *  sends the host-written request (askMessage) into this chat as the owner's
 *  own visible message. It sends the app no event. `status` is the chat's
 *  state for this card (appBlockChat.js askState). */
export function AskControl({ status, locked, onAsk }) {
  const waitingId = useId()
  const waiting = status !== 'ready'
  return <span className="md-app-block__controls">
    <button type="button" className="md-app-block__action is-ask" disabled={locked || waiting}
      aria-describedby={waiting ? waitingId : undefined} onClick={onAsk}>
      {status === 'sending' ? <span className="md-app-block__spinner" aria-hidden="true" />
        : <Chat width={16} height={16} aria-hidden="true" />}
      {ASK_LABELS[status] || ASK_LABEL}</button>
    {waiting ? <span id={waitingId} className="md-app-block__sr-only">{ASK_WAIT[status]}</span> : null}
  </span>
}

/** The passive block for one parsed session state. The parser allows at most
 *  one enabled confirmation; while it is open its app-supplied list is the
 *  block's only rows, with one Not now/Confirm (sessionSnapshot), and
 *  `listEdgeRefs` observes the list's two ends. `ask` is the host's ask
 *  control when one may be drawn; it replaces every app control. */
export function PassiveBlockView({ block, app, state, locked, confirmArmed = false, onEvent, onInternalNav,
  sectionRef = null, confirmRef = null, listEdgeRefs = null, notice = '', onJump = null, ask = null, children = null }) {
  const actionState = key => state?.actions.find(action => action.key === key) || null
  // A hidden action leaves no control, so its footer is not drawn either.
  const actionFor = target => target && !ask && !actionState(target.intent)?.hidden
    ? <SessionControls target={target} state={actionState(target.intent)} locked={locked}
      confirmArmed={confirmArmed} confirmRef={confirmRef} onEvent={onEvent} openHref={block.href} onInternalNav={onInternalNav} />
    : null
  const snapshot = sessionSnapshot(block, state)
  const count = snapshot.confirmation ? snapshot.items.length : 0
  return <AppBlockLayout block={snapshot} app={app} onInternalNav={onInternalNav} ask={ask}
    actionFor={actionFor} summary={state?.summary || ''} sectionRef={sectionRef} listEdgeRefs={listEdgeRefs} onJump={onJump}>
    {/* Always present, so the polite announcement is heard when the rows change. */}
    <p className="md-app-block__sr-only" role="status">
      {count ? `${app.name} lists ${count} ${count === 1 ? 'item' : 'items'} to confirm.` : state?.title || ''}</p>
    {notice ? <p className="md-app-block__notice" role="status">{notice}</p> : null}
    {state?.notice ? <p className="md-app-block__notice" role="status">{state.notice}</p> : null}
    {children}
  </AppBlockLayout>
}

/** No host-request handler: AppCanvas refuses every request from this hidden canvas. */
function SessionHost({ app, session }) {
  return <div className="md-app-block__session-host" aria-hidden="true" inert>
    <Suspense fallback={null}>
      <AppCanvas appId={app.id} appName={app.name} appSlug={app.slug} version={appFrameVersion(app)}
        storageGeneration={app.storage_generation} offlineCapable={app.offline_capable === true}
        capabilityContract={app.capability_contract || null} active={false} visible={false} interactive={false}
        navigationTarget={false} blockSession={session.blockSession} blockEvent={session.event} blockRefresh={session.blockRefresh}
        onBlockState={session.onBlockState} onBlockCapability={session.onBlockCapability} />
    </Suspense>
  </div>
}

/** Bring the newest block into view and move keyboard focus to it, without
 *  animation when the reader prefers reduced motion. */
export function jumpTo(node) {
  if (!node) return
  const reduce = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  node.scrollIntoView({ block: 'center', behavior: reduce ? 'auto' : 'smooth' })
  if (!node.hasAttribute('tabindex')) node.setAttribute('tabindex', '-1')
  node.focus({ preventScroll: true })
}

export const ADMISSION_WITHDRAWN = name =>
  `${name} can no longer run in this chat. Open ${name} to check the result of the last action before trying again.`

export default function PassiveAppBlock({ block, app, onInternalNav, admitted = true }) {
  const [nearRef, { near, distance }] = useNearViewport()
  // The transcript attests which chat this block is in and whether it is that
  // chat's newest block of its app and intent; the block's JSON cannot.
  const chat = useAppBlockChat(appBlockChatKey(block))
  const chatRef = chat.ref
  const sectionRef = useCallback(node => { nearRef(node); chatRef(node) }, [nearRef, chatRef])
  // Observers follow the committed showing (set after it renders).
  const [showing, setShowing] = useState(null)
  const [confirmRef, confirmView] = useOnScreen(showing)
  const [listStartRef, listStartView] = useOnScreen(showing)
  const [listEndRef, listEndView] = useOnScreen(showing)
  const listEdgeRefs = { start: listStartRef, end: listEndRef }
  const [retained, setRetained] = useState(false)
  const [staticOpen, setStaticOpen] = useState(false)
  // Without admission no new frame starts; a retained one keeps its own.
  const granted = usePassiveSlot(app.id, { wanted: admitted && near && !staticOpen, retained, distance })
  const askAllowed = passiveAskAllowed(app)
  const session = useAppBlockSession({ block, near: admitted && near && granted && !staticOpen,
    revision: `${app.passive_block_module_digest}:${appFrameVersion(app)}`,
    confirmView, listStartView, listEndView, admitted, context: chat.context, refresh: chat.refresh, ask: askAllowed })
  useEffect(() => setShowing(session.showing), [session.showing])
  useEffect(() => setRetained(session.retained), [session.retained])
  const ask = blockAsk({ state: session.state, admitted, allowed: askAllowed, context: chat.context })
  const message = ask ? askMessage(block.app, ask) : ''
  if (passiveBlockMode({ admitted, supported: session.supported, near, granted, retained: session.retained, staticOpen }) === 'static') {
    return <StaticAppBlock block={block} app={app} appsLoading={false} onInternalNav={onInternalNav}
      sectionRef={sectionRef} onOpenChange={setStaticOpen} />
  }
  return <PassiveBlockView block={block} app={app} state={session.state} locked={sessionLocked(session, admitted)}
    confirmArmed={admitted && session.confirmArmed} notice={admitted ? '' : ADMISSION_WITHDRAWN(app.name)}
    onEvent={session.dispatch} onInternalNav={onInternalNav} sectionRef={sectionRef}
    confirmRef={confirmRef} listEdgeRefs={listEdgeRefs}
    ask={ask ? <AskControl status={chat.askState()} locked={sessionLocked(session, admitted)}
      onAsk={() => { void chat.ask({ message, fingerprint: blockFingerprint(block), refs: ask.refs }) }} /> : null}
    onJump={chat.context?.latest === false ? () => jumpTo(chat.newestNode()) : null}>
    {session.frameMounted ? <SessionHost app={app} session={session} /> : null}
  </PassiveBlockView>
}
