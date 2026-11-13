/* Session controller for one passive transcript block.

   Everything app-reported (state, acknowledgements) is bound to the exact
   document generation that negotiated support. A new document resets idle
   controls; releasing an idle offscreen frame discards its authority. Its last
   idle presentation stays visible, locked until the new document reports.

   When Confirm may be pressed is decided by useConfirmArming. An unsupported
   negotiation belongs to one applied revision: a newer revision is tried
   again. */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import useConfirmArming from './useConfirmArming.js'
import { blockActions, blockItems } from '../markdown/appBlock.js'
import {
  ASK_FEATURE, SESSION_FEATURES, sessionDocumentReset, sessionRetained, sessionState, sessionStateUpdate,
} from '../markdown/appBlockSession.js'

const UNNEGOTIATED = Object.freeze({ generation: null, supported: null })
export { CONFIRM_ARM_MS } from './useConfirmArming.js'

export default function useAppBlockSession({ block, near, revision = null, confirmView = null, listStartView = null,
  listEndView = null, admitted = true, context = null, refresh = null, ask = false }) {
  const [sessionId] = useState(() => crypto.randomUUID())
  const [state, setState] = useState(null)
  // Presentation is not session authority: never replay it into the app or
  // use it for retention, confirmation, asks or dispatch. Scope it to this
  // exact block, applied revision and host-attested chat position.
  const presentationScope = JSON.stringify([block, revision, context?.chatId, context?.latest, admitted])
  const [presentation, setPresentation] = useState(null)
  const reportedRef = useRef(false)
  const [event, setEvent] = useState(null)
  const eventRef = useRef(null)
  const [negotiation, setNegotiation] = useState(UNNEGOTIATED)
  // Callbacks read the latest negotiation synchronously, before React commits.
  const negotiationRef = useRef(UNNEGOTIATED)
  const actions = useMemo(() => blockActions(block), [block])
  const items = useMemo(() => blockItems(block), [block])
  const keys = useMemo(() => new Set(actions.map(action => action.key)), [actions])
  const intents = useMemo(() => new Set(items.map(item => item.intent)), [items])
  // `ask-v1` is advertised, and an ask accepted, only for an app admitted to
  // `chat.blocks.ask` (appBlockSession.js passiveAskAllowed).
  const features = useMemo(() => ask ? [...SESSION_FEATURES, ASK_FEATURE] : SESSION_FEATURES, [ask])
  const arming = useConfirmArming({ state, generation: negotiation.generation, supported: negotiation.supported,
    confirmView, listStartView, listEndView })
  const { disarm, noteReport, mayConfirm } = arming
  const retained = sessionRetained(state, event)
  const frameWanted = near || retained
  // An unsupported document still keeps unresolved ownership mounted.
  const frameMounted = frameWanted && (negotiation.supported !== false || retained)

  const admittedRef = useRef(admitted)
  useLayoutEffect(() => { admittedRef.current = admitted }, [admitted])

  const clearEvent = useCallback(() => {
    eventRef.current = null
    setEvent(null)
  }, [])

  // A newer applied revision may support sessions: forget an unsupported
  // verdict from the previous one (an unresolved retained session is kept).
  const revisionRef = useRef(revision)
  useEffect(() => {
    if (revisionRef.current === revision) return
    revisionRef.current = revision
    if (negotiationRef.current.supported !== false || retained) return
    negotiationRef.current = UNNEGOTIATED
    setNegotiation(UNNEGOTIATED)
  }, [revision, retained])

  // Idle sessions are released offscreen; no negotiated authority survives.
  // Only their read-only presentation can remain. An unsupported revision stays a static block.
  useEffect(() => {
    if (frameWanted || negotiationRef.current.supported === false) return
    disarm()
    reportedRef.current = false
    negotiationRef.current = UNNEGOTIATED
    setNegotiation(UNNEGOTIATED)
    setState(null)
    clearEvent()
  }, [frameWanted, clearEvent, disarm])

  const onBlockCapability = useCallback((supported, { generation, reset }) => {
    if (generation !== negotiationRef.current.generation || reset) {
      disarm()
      reportedRef.current = false
    }
    if (reset) {
      // Capture before clearing: the updater may run after the ref changes.
      const previous = eventRef.current
      setState(current => sessionDocumentReset(current, previous))
      clearEvent()
    }
    const next = { generation, supported }
    negotiationRef.current = next
    setNegotiation(next)
  }, [clearEvent, disarm])

  const onBlockState = useCallback((message, { generation }) => {
    const current = negotiationRef.current
    if (current.supported !== true || generation !== current.generation) return
    const safe = sessionState(message, sessionId, keys, intents, block.app, { ask })
    if (!safe) return
    reportedRef.current = true
    setPresentation(sessionRetained(safe, null) ? null : { scope: presentationScope, state: { ...safe, ask: null } })
    noteReport(safe)
    setState(previous => sessionStateUpdate(previous, safe))
    if (!safe.checkpointInvalid && safe.ackNonce && safe.ackNonce === eventRef.current?.nonce) clearEvent()
  }, [sessionId, keys, intents, block.app, ask, clearEvent, noteReport, presentationScope])

  // Reserve synchronously: a second click before React commits, or before
  // the app acknowledges this exact nonce, cannot send another event.
  const dispatch = useCallback((key, kind) => {
    const current = negotiationRef.current
    // Withdrawn admission: the session keeps settling, but takes no new event.
    if (!admittedRef.current || !reportedRef.current || eventRef.current || current.supported !== true || !keys.has(key)) return false
    if (kind === 'confirm' && !mayConfirm(key)) return false
    // Every owner event ends the showing, Confirm included: a press spends
    // it, so another confirm needs a new showing and a fresh visible delay.
    disarm()
    const message = { sessionId, generation: current.generation, key, event: kind, nonce: crypto.randomUUID() }
    setPresentation(null)
    eventRef.current = message
    setEvent(message)
    return true
  }, [sessionId, keys, disarm, mayConfirm])

  // Scalar dependencies: the parser copies envelopes on every message even
  // when their observational data is unchanged.
  const checkpointId = state?.checkpoint?.id ?? null
  const checkpointData = state?.checkpoint?.data ?? null
  const recoveryError = state?.recoveryError ?? null
  // Host-attested: which chat renders this block and whether it is that
  // chat's newest block of its app and intent. Never read from block JSON.
  const chatId = typeof context?.chatId === 'string' && context.chatId ? context.chatId : null
  const latest = context?.latest !== false
  // `intent` names the group `latest` is computed in: this block's app and
  // its parsed block intent.
  const blockIntent = block.intent
  const blockSession = useMemo(() => ({
    sessionId, actions, items, features, retain: retained, recoveryError,
    checkpoint: checkpointId ? { id: checkpointId, data: checkpointData } : null,
    ...(chatId ? { context: { chatId, latest, intent: blockIntent } } : {}),
  }), [sessionId, actions, items, features, retained, recoveryError, checkpointId, checkpointData, chatId, latest, blockIntent])

  // A refresh hint reaches only a document that is mounted and supported when
  // the hint is raised; a document that loads later reads on its own start.
  const [blockRefresh, setBlockRefresh] = useState(null)
  const frameMountedRef = useRef(frameMounted)
  frameMountedRef.current = frameMounted
  const refreshSeq = refresh?.seq ?? 0
  const refreshReason = refresh?.reason ?? ''
  const seenRefreshRef = useRef(refreshSeq)
  useEffect(() => {
    if (refreshSeq === seenRefreshRef.current) return
    seenRefreshRef.current = refreshSeq
    const current = negotiationRef.current
    if (current.supported !== true || !frameMountedRef.current) return
    setBlockRefresh({ seq: refreshSeq, reason: refreshReason, sessionId, generation: current.generation })
  }, [refreshSeq, refreshReason, sessionId])

  return {
    sessionId, state, displayState: state || (presentation?.scope === presentationScope ? presentation.state : null),
    event, awaitingAck: event !== null, supported: negotiation.supported, confirmArmed: arming.armed, showing: arming.showing,
    generation: negotiation.generation, retained, frameMounted, blockSession, blockRefresh,
    onBlockCapability, onBlockState, dispatch,
  }
}
