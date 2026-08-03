import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { X } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import { captureLayoutSpace, clientDeltaToLayout } from '../../lib/layoutSpace.js'
import {
  autopilotOnSend,
  contributeApp as findContributeApp,
  contributeAppId,
  contributeLabel,
  diffStatSummary,
  isHorizontalSwipe,
  passedDismissThreshold,
  payoffLine,
  rememberReviewItemDismissed,
  reviewPanelSummary,
  statusLabel,
  submitFailure,
  visibleReviewItems,
} from './contributionReviewModel.js'
import './ContributionReviewCard.css'

export default function ContributionReviewCard({ chatId, turnActive, onOpenApp }) {
  const queryClient = useQueryClient()
  const { data: apps } = appQueries.list.useQuery()
  const appId = contributeAppId(apps)
  const contributeApp = findContributeApp(apps, appId)

  const queryKey = useMemo(
    () => ['contributions-for-chat', appId, chatId],
    [appId, chatId],
  )
  // Read-only. A 404 (older backend) resolves to null and the card stays hidden.
  const { data } = useQuery({
    queryKey,
    queryFn: () => api.contributions.forChat(appId, chatId)
      .then(r => (r.ok ? r.json() : null)),
    enabled: !!appId && !!chatId,
    staleTime: 15000,
    retry: false,
  })

  // The agent stages a review during a turn, so refetch exactly once when a turn
  // SETTLES — not on every render and not on a timer. Nothing else can add a
  // record for this chat behind the owner's back.
  const wasActive = useRef(turnActive)
  useEffect(() => {
    if (wasActive.current && !turnActive) {
      queryClient.invalidateQueries({ queryKey, exact: true })
    }
    wasActive.current = turnActive
  }, [turnActive, queryClient, queryKey])

  // The server remains authoritative, but a successful item leaves this
  // composer surface immediately rather than waiting for its ledger refetch.
  const [contributedItems, setContributedItems] = useState([])
  // Dismissals are persisted, so this only forces the re-render; the stored
  // decision is what actually filters the list.
  const [dismissRevision, setDismissRevision] = useState(0)

  const storage = typeof localStorage !== 'undefined' ? localStorage : null
  // Remove a successful single record locally before the ledger refetch
  // returns. Stack review items never send from chat and stay untouched.
  const pendingItems = visibleReviewItems(data, storage).filter(
    item => item.kind !== 'record'
      || !contributedItems.some(done => done.id === item.record.id),
  )
  const panel = reviewPanelSummary(pendingItems.length)
  const grouped = panel.count > 1
  void dismissRevision
  if (!appId) return null
  if (panel.count === 0) return null

  async function submit(record) {
    try {
      const res = await api.contributions.submit(appId, record.id, {
        autopilot: autopilotOnSend(data),
      })
      const body = await res.json().catch(() => null)
      if (!res.ok) {
        const detail = body?.detail
        const message = typeof detail === 'string' ? detail : detail?.message
        return {
          failure: {
            message: typeof message === 'string' && message
              ? message
              : 'Could not contribute this. Open Contribute for the details.',
            detail: typeof detail?.detail === 'string' ? detail.detail : '',
          },
        }
      }
      return { contributed: true }
    } catch {
      return {
        failure: {
          message: 'Could not reach the server. Nothing was contributed.',
          detail: '',
        },
      }
    } finally {
      queryClient.invalidateQueries({ queryKey, exact: true })
    }
  }

  function rememberContributed(recordId) {
    setContributedItems(items => (
      items.some(item => item.id === recordId)
        ? items
        : [...items, { id: recordId }]
    ))
  }

  return (
    <div
      className={`contrib-card-stack${grouped ? ' contrib-card-stack--grouped' : ''}`}
      role={grouped ? 'region' : undefined}
      aria-label={grouped ? panel.title : undefined}
    >
      {grouped && (
        <div className="contrib-card-stack__heading">
          <div>
            <div className="contrib-card-stack__title">
              {panel.title}
            </div>
            <div className="contrib-card-stack__copy">
              {panel.copy}
            </div>
          </div>
          <span className="contrib-card-stack__count">{panel.count}</span>
        </div>
      )}
      {pendingItems.map(item => {
        const onOpenContribute = contributeApp && onOpenApp
          ? () => onOpenApp(contributeApp, { final: true })
          : null
        const onDismiss = () => {
          rememberReviewItemDismissed(item, storage)
          setDismissRevision(value => value + 1)
        }
        if (item.kind === 'stack') {
          return (
            <StackReviewRow
              key={item.id}
              item={item}
              onOpenContribute={onOpenContribute}
              onDismiss={onDismiss}
            />
          )
        }
        const record = item.record
        return (
          <ReviewRow
            key={item.id}
            record={record}
            autopilot={autopilotOnSend(data)}
            showPayoff={!grouped}
            onSubmit={submit}
            onContributed={rememberContributed}
            onOpenContribute={onOpenContribute}
            onDismiss={onDismiss}
          />
        )
      })}
    </div>
  )
}


/**
 * Swipe-to-dismiss, either direction, for any card shape in this file.
 *
 * Bound NATIVELY with a non-passive touchmove for the same reason the navigation
 * drawer's handlers are: React's touch props are passive, so they can watch a
 * gesture but never claim it, and `touch-action` cannot cover for that on iOS
 * (WebKit does not implement the pan-* keywords). Claiming is what stops the
 * surface underneath from taking the drag.
 *
 * It lives as a hook rather than inside one card because every actionable card
 * here needs the same exit.
 */
function useSwipeToDismiss(onDismiss) {
  const cardRef = useRef(null)
  const swipe = useRef({ x: 0, y: 0, active: false, claimed: false, layoutSpace: null })
  const dismissRef = useRef(onDismiss)
  dismissRef.current = onDismiss

  useEffect(() => {
    const el = cardRef.current
    if (!el) return undefined
    let dismissTimer = null

    const clear = () => {
      el.classList.remove('contrib-card--dragging')
      el.style.transform = ''
      el.style.opacity = ''
    }
    const onStart = (event) => {
      if (event.touches.length !== 1) { swipe.current.active = false; return }
      swipe.current = {
        x: event.touches[0].clientX, y: event.touches[0].clientY,
        active: true, claimed: false,
        layoutSpace: captureLayoutSpace(el),
      }
    }
    const onMove = (event) => {
      const state = swipe.current
      if (!state.active || event.touches.length !== 1) return
      const dx = event.touches[0].clientX - state.x
      const dy = event.touches[0].clientY - state.y
      // Vertical movement belongs to the expanded details scroller; only a
      // decisively sideways drag becomes a dismissal, and once claimed it stays
      // claimed for the rest of the gesture.
      if (!state.claimed && !isHorizontalSwipe(dx, dy)) return
      state.claimed = true
      event.preventDefault()
      el.classList.add('contrib-card--dragging')
      const layoutDx = clientDeltaToLayout(
        { x: dx, y: 0 },
        state.layoutSpace,
      ).x
      el.style.transform = `translateX(${layoutDx}px)`
      el.style.opacity = String(Math.max(0.3, 1 - Math.abs(dx) / 260))
    }
    const onEnd = (event) => {
      const state = swipe.current
      state.active = false
      if (!state.claimed) return
      state.claimed = false
      const touch = event.changedTouches[0]
      const dx = touch.clientX - state.x
      const dy = touch.clientY - state.y
      el.classList.remove('contrib-card--dragging')
      if (passedDismissThreshold(dx, dy)) {
        el.style.transform = `translateX(${dx > 0 ? '110%' : '-110%'})`
        el.style.opacity = '0'
        dismissTimer = window.setTimeout(() => dismissRef.current?.(), 160)
        return
      }
      clear()
    }
    const onCancel = () => {
      swipe.current.active = false
      swipe.current.claimed = false
      clear()
    }

    el.addEventListener('touchstart', onStart, { passive: true })
    el.addEventListener('touchmove', onMove, { passive: false })
    el.addEventListener('touchend', onEnd, { passive: true })
    el.addEventListener('touchcancel', onCancel, { passive: true })
    return () => {
      el.removeEventListener('touchstart', onStart)
      el.removeEventListener('touchmove', onMove)
      el.removeEventListener('touchend', onEnd)
      el.removeEventListener('touchcancel', onCancel)
      if (dismissTimer !== null) window.clearTimeout(dismissTimer)
    }
  }, [])

  return cardRef
}


function StackReviewRow({ item, onOpenContribute, onDismiss }) {
  const [open, setOpen] = useState(false)
  const cardRef = useSwipeToDismiss(onDismiss)
  const total = Number(item.stack?.total) || item.records.length
  const name = item.stack?.name || 'This improvement'

  return (
    <div ref={cardRef} className="contrib-card contrib-card--stack">
      <div className="contrib-card__badge">
        <span>Review together</span>
        <button
          type="button"
          className="contrib-card__dismiss"
          aria-label="Dismiss — keeps the stack in Contribute"
          onClick={() => onDismiss?.()}
        >
          <X width={14} height={14} aria-hidden="true" />
        </button>
      </div>
      <p className="contrib-card__summary">
        {name} is ready as a {total}-part contribution stack.
      </p>
      <p className="contrib-card__meta">
        {item.repo} · {item.records.length} linked layer{item.records.length === 1 ? '' : 's'}
      </p>
      <p className="contrib-card__payoff">
        The layers build on each other and are reviewed in order.
      </p>
      <div className="contrib-card__actions">
        <button
          type="button"
          className="contrib-card__send"
          disabled={!onOpenContribute}
          onClick={onOpenContribute}
        >
          Review in Contribute
        </button>
        <button
          type="button"
          className="contrib-card__toggle"
          aria-expanded={open}
          onClick={() => setOpen(value => !value)}
        >
          {open ? 'Hide layers' : 'Layers'}
        </button>
      </div>
      {open && (
        <div className="contrib-card__details">
          <ol className="contrib-card__layers">
            {item.records.map((record, index) => (
              <li key={record.id}>
                <span>{record.stack?.position || index + 1}. {record.title}</span>
                {record.summary && <p>{record.summary}</p>}
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  )
}

function ReviewRow({
  record, autopilot, showPayoff, onSubmit, onContributed,
  onOpenContribute, onDismiss,
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [attempt, setAttempt] = useState(null)
  // Each row owns its own in-flight guard. That keeps sibling rows independent
  // while closing the same-frame double-click window for this record.
  const activeSendRef = useRef(false)
  const diffStat = diffStatSummary(record.diff_stat)
  const submitting = record.status === 'submitting'
  const cardRef = useSwipeToDismiss(onDismiss)
  const failure = submitFailure(record, { attempt, sending: busy || submitting })

  async function send() {
    if (activeSendRef.current) return
    activeSendRef.current = true
    setBusy(true)
    setAttempt(null)
    let outcome
    try {
      outcome = await onSubmit(record)
    } finally {
      activeSendRef.current = false
      setBusy(false)
    }
    if (outcome?.failure) {
      setAttempt(outcome.failure)
    } else if (outcome?.contributed) {
      onContributed(record.id)
    }
  }

  return (
    <div ref={cardRef} className="contrib-card">
      {/* Header band, then what it is, then the actions. The band also carries
          dismissal, so the swipe has a visible, pointer- and keyboard-reachable
          equivalent rather than being a touch-only secret. */}
      <div className="contrib-card__badge">
        <span>{statusLabel(record)}</span>
        <button
          type="button"
          className="contrib-card__dismiss"
          aria-label="Dismiss — keeps it in Contribute"
          onClick={() => onDismiss?.()}
        >
          <X width={14} height={14} aria-hidden="true" />
        </button>
      </div>
      <p className="contrib-card__summary">
        {record.summary || record.title || 'An improvement is ready to contribute'}
      </p>

      <p className="contrib-card__meta">
        {record.repo}
        {diffStat ? <span> · {diffStat}</span> : null}
      </p>

      {showPayoff && !failure && !submitting && (
        <p className="contrib-card__payoff">{payoffLine(record)}</p>
      )}
      {autopilot && !failure && !submitting && (
        <p className="contrib-card__meta">
          Möbius will also handle review feedback after you contribute.
        </p>
      )}
      {/* One sentence says what happened; the transcript that proves it stays
          collapsed next to it, so the card explains without shouting plumbing
          at someone who only needs to know nothing was published. */}
      {failure && <p className="contrib-card__error">{failure.message}</p>}
      {failure?.detail && (
        <details className="contrib-card__failure-detail">
          <summary>What blocked it</summary>
          <pre className="contrib-card__body">{failure.detail}</pre>
        </details>
      )}

      <div className="contrib-card__actions">
        <button
          type="button"
          className="contrib-card__send"
          disabled={busy || submitting}
          onClick={send}
        >
          {submitting || busy ? 'Contributing…' : contributeLabel(record)}
        </button>
        <button
          type="button"
          className="contrib-card__toggle"
          aria-expanded={open}
          onClick={() => setOpen(value => !value)}
        >
          {open ? 'Hide details' : 'Details'}
        </button>
        {/* A failed submit may reveal a server-side change after this ready
            card loaded. Contribute owns that repair path. */}
        {failure && onOpenContribute && (
          <button
            type="button"
            className="contrib-card__toggle"
            onClick={onOpenContribute}
          >
            Open Contribute
          </button>
        )}
      </div>

      {open && (
        <div className="contrib-card__details">
          {record.title && (
            <p className="contrib-card__detail-title">{record.title}</p>
          )}
          {record.branch && (
            <p className="contrib-card__detail-line">Branch {record.branch}</p>
          )}
          {record.labels?.length > 0 && (
            <p className="contrib-card__detail-line">
              Labels {record.labels.join(', ')}
            </p>
          )}
          {record.files?.length > 0 && (
            <>
              <p className="contrib-card__detail-label">Files</p>
              <ul className="contrib-card__files">
                {record.files.map(file => <li key={file}>{file}</li>)}
              </ul>
            </>
          )}
          {record.body_draft && (
            <>
              <p className="contrib-card__detail-label">
                The exact text that will be published
              </p>
              <pre className="contrib-card__body">{record.body_draft}</pre>
            </>
          )}
        </div>
      )}
    </div>
  )
}
