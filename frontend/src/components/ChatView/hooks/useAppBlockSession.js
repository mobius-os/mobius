/* Session controller for one passive transcript block.

   Everything app-reported (state, acknowledgements) is bound to the exact
   document generation that negotiated support. A new document resets idle
   controls; releasing an idle offscreen frame discards its authority. Its last
   idle presentation stays visible without restarting on scroll. A tap wakes
   a read-only preparation; only a current report can authorize dispatch.

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

export default function useAppBlockSession({ block, near, granted = true, revision = null, confirmView = null, listStartView = null,
  listEndView = null, admitted = true, context = null, refresh = null, ask = false }) {
  const [sessionId] = useState(() => crypto.randomUUID())
  const [state, setState] = useState(null)
  // Presentation is not session authority: never replay it into the app or
  // use it for retention, confirmation, asks or dispatch. Scope it to this
  // exact block, applied revision and host-attested chat position.
  const presentationScope = JSON.stringify([block, revision, context?.chatId, context?.latest, admitted])
  const [presentation, setPresentation] = useState(null)
  const refreshSeq = refresh?.seq ?? 0
  const executionScope = JSON.stringify([presentationScope, refreshSeq])
  const saved = presentation?.scope === presentationScope ? presentation : null
  const [activation, setActivation] = useState(null)
  const activationRef = useRef(null)
  const requestedActivation = activation?.scope === presentationScope ? activation : null
  const reportedRef = useRef(false)
  const reportedScope = useRef(null)
  const reportedRefresh = useRef(null)
  const generationScope = useRef(null)
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
  const authoritative = reportedRef.current && reportedScope.current === presentationScope && reportedRefresh.current === refreshSeq
  const arming = useConfirmArming({ state: authoritative ? state : null, generation: negotiation.generation, supported: negotiation.supported,
    confirmView, listStartView, listEndView })
  const { disarm, noteReport, mayConfirm } = arming
  const retained = sessionRetained(state, event)
  const [documentScope, setDocumentScope] = useState(executionScope)
  useEffect(() => { if (!retained) setDocumentScope(executionScope) }, [retained, executionScope])
  // Viewport entry starts the first read, not another read of an idle card.
  // A refresh invalidates the observation even while no document is mounted.
  const wantsFrame = near && (!saved || saved.refreshSeq !== refreshSeq || negotiation.generation !== null || Boolean(requestedActivation))
  const frameWanted = wantsFrame && granted || retained
  // An unsupported document still keeps unresolved ownership mounted.
  const frameMounted = frameWanted && (negotiation.supported !== false || retained)

  const admittedRef = useRef(admitted)
  useLayoutEffect(() => { admittedRef.current = admitted }, [admitted])

  const clearEvent = useCallback(() => {
    eventRef.current = null
    setEvent(null)
  }, [])

  // An idle confirmation has no external outcome to recover. Invalidating
  // its observation cancels that showing; only unresolved effects retain the
  // old document until it settles.
  useEffect(() => {
    if (documentScope === executionScope || sessionDocumentReset(state, eventRef.current)) return
    disarm()
    reportedRef.current = false
    setState(null)
    clearEvent()
  }, [documentScope, executionScope, state, disarm, clearEvent])

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
    generationScope.current = documentScope
    const next = { generation, supported }
    negotiationRef.current = next
    setNegotiation(next)
  }, [clearEvent, disarm, documentScope])

  const onBlockState = useCallback((message, { generation }) => {
    const current = negotiationRef.current
    if (current.supported !== true || generation !== current.generation
      || generationScope.current !== executionScope && !retained) return
    const safe = sessionState(message, sessionId, keys, intents, block.app, { ask })
    if (!safe) return
    const currentObservation = generationScope.current === executionScope
    reportedScope.current = currentObservation ? presentationScope : null
    reportedRefresh.current = currentObservation ? refreshSeq : null
    reportedRef.current = !safe.loading
    if (sessionRetained(safe, null)) setPresentation(null)
    else if (!safe.loading && currentObservation) setPresentation({ scope: presentationScope, refreshSeq, state: sessionStateUpdate(null, safe) })
    noteReport(safe)
    setState(previous => sessionStateUpdate(previous, safe))
    if (!safe.checkpointInvalid && safe.ackNonce && safe.ackNonce === eventRef.current?.nonce) clearEvent()
  }, [sessionId, keys, intents, block.app, ask, clearEvent, noteReport, presentationScope, refreshSeq, retained, executionScope])

  // Reserve synchronously: a second click before React commits, or before
  // the app acknowledges this exact nonce, cannot send another event.
  const dispatch = useCallback((key, kind) => {
    const current = negotiationRef.current
    // Withdrawn admission: the session keeps settling, but takes no new event.
    if (!admittedRef.current || !reportedRef.current || reportedScope.current !== presentationScope
      || reportedRefresh.current !== refreshSeq || context?.latest === false || eventRef.current || current.supported !== true || !keys.has(key)) return false
    if (kind === 'confirm' && !mayConfirm(key)) return false
    // Every owner event ends the showing, Confirm included: a press spends
    // it, so another confirm needs a new showing and a fresh visible delay.
    disarm()
    const message = { sessionId, generation: current.generation, key, event: kind, nonce: crypto.randomUUID() }
    setPresentation(null)
    eventRef.current = message
    setEvent(message)
    return true
  }, [sessionId, keys, disarm, mayConfirm, presentationScope, refreshSeq, context?.latest])

  // Only a read-only activation can cross a sleeping document boundary.
  // Confirmation, cancellation and asks always require current authority.
  const requestEvent = useCallback((key, kind) => {
    if (authoritative) return dispatch(key, kind)
    const action = saved?.state.actions.find(item => item.key === key)
    if (kind !== 'activate' || !admitted || !keys.has(key) || !action || action.disabled
      || action.hidden || action.open || activationRef.current || eventRef.current) return false
    const request = { key, scope: presentationScope }
    activationRef.current = request
    setActivation(request)
    return true
  }, [dispatch, saved, admitted, keys, presentationScope, authoritative])

  const requestAsk = useCallback(onReady => {
    if (!admitted || activationRef.current || eventRef.current) return false
    if (authoritative && context?.latest !== false && state?.ask && !state.recoveryPending) { onReady(state.ask); return true }
    if (!saved?.state.ask || state?.loading) return false
    const request = { scope: presentationScope, ask: saved.state.ask, onReady }
    activationRef.current = request
    setActivation(request)
    return true
  }, [admitted, state, saved, presentationScope, authoritative, context?.latest])

  useEffect(() => {
    if (!activation) return
    if (activation.scope !== presentationScope || !admitted || negotiation.supported === false) {
      activationRef.current = null
      setActivation(null)
      return
    }
    if (!authoritative || !state || state.loading) return
    activationRef.current = null
    setActivation(null)
    if (activation.ask) {
      if (!state.recoveryPending && !state.recoveryError && state.ask
        && JSON.stringify(state.ask) === JSON.stringify(activation.ask)) activation.onReady(state.ask)
      return
    }
    const action = state.actions.find(item => item.key === activation.key)
    if (!state.ask && !state.recoveryPending && !state.recoveryError && action && !action.disabled
      && !action.hidden && !action.open && !action.busy && !action.confirming) dispatch(activation.key, 'activate')
  }, [activation, presentationScope, admitted, negotiation.supported, state, dispatch, authoritative])

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

  return {
    sessionId, state, displayState: state && (retained || reportedScope.current === presentationScope && !state.loading) ? state : saved?.state || null,
    wantsFrame, requestEvent, requestAsk, documentScope, authoritative,
    canWake: admitted && Boolean(saved) && !state && !requestedActivation && negotiation.supported !== false,
    event, awaitingAck: event !== null || Boolean(requestedActivation), supported: negotiation.supported, confirmArmed: arming.armed, showing: arming.showing,
    generation: negotiation.generation, retained, frameMounted, blockSession,
    onBlockCapability, onBlockState, dispatch,
  }
}
