import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { X } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import { captureLayoutSpace, clientLengthToLayout } from '../../lib/layoutSpace.js'
import {
  autopilotOnSend,
  contributeApp as findContributeApp,
  contributeAppId,
  contributionRecoveryAction,
  contributionReviewRunPhase,
  contributionReviewIntent,
  diffStatSummary,
  isTrackingRecord,
  isHorizontalSwipe,
  passedDismissThreshold,
  publicationAction,
  rememberReviewItemDismissed,
  reviewDestinationLabel,
  reviewItemIntent,
  reviewPanelSummary,
  sendBlocker,
  statusLabel,
  submitFailure,
  trackingNarration,
  trackingStatusLabel,
  visibleReviewItems,
} from './contributionReviewModel.js'
import './ContributionReviewCard.css'

export default function ContributionReviewCard({
  chatId, turnActive, onOpenApp, onOpenChat, onContinueInChat,
}) {
  const queryClient = useQueryClient()
  const { data: apps } = appQueries.list.useQuery()
  const appId = contributeAppId(apps)
  const contributeApp = findContributeApp(apps, appId)
  const { data: appToken } = appQueries.token.useQuery(appId)

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

  // Dismissals are persisted, so this only forces the re-render; the stored
  // decision is what actually filters the list.
  const [dismissRevision, setDismissRevision] = useState(0)

  const storage = typeof localStorage !== 'undefined' ? localStorage : null
  const pendingItems = visibleReviewItems(data, storage)
  const panel = reviewPanelSummary(pendingItems)
  const grouped = panel.count > 1
  void dismissRevision
  if (!appId) return null
  if (panel.count === 0) return null

  async function publish(record) {
    try {
      const response = await api.contributions.publish(appId, record, {
        autopilot: autopilotOnSend(data),
      })
      const body = await response.json().catch(() => null)
      if (!response.ok) {
        const detail = body?.detail
        const message = typeof detail === 'string' ? detail : detail?.message
        return {
          failure: {
            message: typeof message === 'string' && message
              ? message
              : 'Could not send this pull request. Open Contribute for details.',
            detail: typeof detail?.detail === 'string' ? detail.detail : '',
          },
        }
      }
      return { published: true, publication: body }
    } catch {
      return {
        failure: {
          message: 'Could not reach the server. Nothing was sent.',
          detail: '',
        },
      }
    } finally {
      queryClient.invalidateQueries({ queryKey, exact: true })
    }
  }

  function rememberPublished(recordId, publication = null) {
    const publishedStatus = publication?.record?.status === 'draft' ? 'draft' : 'open'
    queryClient.setQueryData(queryKey, current => {
      if (!current || !Array.isArray(current.records)) return current
      return {
        ...current,
        records: current.records.map(record => (
          record.id === recordId
            ? {
                ...record,
                status: publishedStatus,
                number: publication?.number ?? record.number,
                url: publication?.url ?? record.url,
                needs_attention: false,
              }
            : record
        )),
      }
    })
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
        </div>
      )}
      {pendingItems.map(item => {
        const onDismiss = () => {
          rememberReviewItemDismissed(item, storage)
          setDismissRevision(value => value + 1)
        }
        // Opening the durable review consumes this version-scoped doorway. A
        // revised review gets a new dismissal identity and can surface again.
        const onOpenContribute = contributeApp && onOpenApp
          ? (intent) => {
              onOpenApp(contributeApp, { final: true, intent })
              onDismiss()
            }
          : null
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
        if (isTrackingRecord(record)) {
          return (
            <TrackingRow
              key={item.id}
              record={record}
              turnActive={turnActive}
              onContinueInChat={onContinueInChat}
              onDismiss={onDismiss}
            />
          )
        }
        return (
          <ReviewRow
            key={item.id}
            record={record}
            connected={data?.connected !== false}
            onPublish={publish}
            onPublished={rememberPublished}
            appToken={appToken}
            onOpenChat={onOpenChat}
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
      const layoutDx = clientLengthToLayout(dx, state.layoutSpace)
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
  const cardRef = useSwipeToDismiss(onDismiss)
  const total = Number(item.stack?.total) || item.records.length
  const name = item.stack?.name || 'This improvement'
  const intent = reviewItemIntent(item)

  return (
    <div ref={cardRef} className="contrib-card contrib-card--stack">
      <div className="contrib-card__topline">
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
      {item.repo ? <p className="contrib-card__meta">{item.repo}</p> : null}
      <p className="contrib-card__payoff">
        Contribute keeps the layers in order and opens one decision.
      </p>
      <div className="contrib-card__actions">
        <button
          type="button"
          className="contrib-card__send"
          disabled={!onOpenContribute || !intent}
          onClick={() => onOpenContribute?.(intent)}
        >
          Review stack in Contribute
        </button>
      </div>
    </div>
  )
}

function TrackingRow({ record, turnActive, onContinueInChat, onDismiss }) {
  const cardRef = useSwipeToDismiss(onDismiss)
  const canContinue = record.needs_attention === true
    && typeof onContinueInChat === 'function'
  const number = Number(record.number)
  const meta = [
    record.repo,
    Number.isInteger(number) && number > 0 ? `PR #${number}` : '',
  ].filter(Boolean).join(' · ')

  return (
    <div ref={cardRef} className="contrib-card contrib-card--tracking">
      <div className="contrib-card__topline">
        <span>{trackingStatusLabel(record)}</span>
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
        {record.summary || record.title || 'Contribution from this chat'}
      </p>
      {meta ? <p className="contrib-card__meta">{meta}</p> : null}
      <p className={record.needs_attention ? 'contrib-card__error' : 'contrib-card__payoff'}>
        {trackingNarration(record)}
      </p>
      {canContinue ? (
        <div className="contrib-card__actions">
          <button
            type="button"
            className="contrib-card__send"
            onClick={() => onContinueInChat(record)}
          >
            {turnActive ? 'Queue agent follow-up' : 'Ask agent to fix'}
          </button>
        </div>
      ) : null}
    </div>
  )
}


function ReviewRow({
  record, connected, onPublish, onPublished, appToken, onOpenChat,
  onOpenContribute, onDismiss,
}) {
  const [sending, setSending] = useState(false)
  const [attemptFailure, setAttemptFailure] = useState(null)
  const [startingReview, setStartingReview] = useState(false)
  const [reviewRun, setReviewRun] = useState(null)
  const [reviewStartError, setReviewStartError] = useState('')
  const diffStat = diffStatSummary(record.diff_stat)
  const submitting = record.status === 'submitting'
  const busy = sending || submitting
  const cardRef = useSwipeToDismiss(onDismiss)
  const failure = submitFailure(record, {
    attempt: attemptFailure,
    sending: busy,
  })
  const intent = contributionReviewIntent(record)
  const blocker = sendBlocker(record, { connected })
  const action = publicationAction(record)
  const recovery = contributionRecoveryAction(record)
  const reviewQueryKey = ['contribution-review-run', recovery?.scope || 'none']
  const { data: existingReview, isLoading: checkingReview } = useQuery({
    queryKey: reviewQueryKey,
    enabled: !!failure && !!appToken && !!recovery?.scope,
    staleTime: 5000,
    retry: false,
    queryFn: async () => {
      const response = await api.appChats.listWithToken(appToken, {
        scope: recovery.scope,
      })
      if (!response.ok) throw new Error('Could not inspect app-owned reviews')
      const rows = await response.json()
      const chat = Array.isArray(rows) ? rows[0] : null
      if (!chat?.id) return null
      const runtimeResponse = await api.chats.runtime(chat.id, { timeoutMs: 5000 })
      const runtime = runtimeResponse.ok ? await runtimeResponse.json() : null
      return {
        chatId: String(chat.id),
        phase: contributionReviewRunPhase(runtime),
      }
    },
  })
  const resolvedReview = reviewRun || existingReview

  async function publish() {
    if (sending || blocker || typeof onPublish !== 'function') return
    setSending(true)
    setAttemptFailure(null)
    try {
      const outcome = (await onPublish(record)) || {}
      if (outcome.published) onPublished?.(record.id, outcome.publication)
      else if (outcome.failure) setAttemptFailure(outcome.failure)
    } finally {
      setSending(false)
    }
  }

  async function startReview() {
    if (!appToken || !recovery || startingReview || resolvedReview?.chatId) return
    setStartingReview(true)
    setReviewStartError('')
    try {
      let timezone = 'UTC'
      try { timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' } catch {}
      const response = await api.appChats.startWithToken(appToken, {
        title: recovery.title,
        scope: recovery.scope,
        scope_label: recovery.scopeLabel,
        owner_visible: true,
        content: recovery.draft,
        cid: typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
          ? crypto.randomUUID()
          : `cid-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
        timezone,
      })
      const body = await response.json().catch(() => null)
      if (!response.ok || !body?.chat_id) throw new Error('Review did not start')
      let phase = 'running'
      const runtimeResponse = await api.chats.runtime(body.chat_id, { timeoutMs: 5000 })
        .catch(() => null)
      if (runtimeResponse?.ok) {
        phase = contributionReviewRunPhase(await runtimeResponse.json())
      }
      setReviewRun({ chatId: String(body.chat_id), phase })
    } catch {
      setReviewStartError('Could not start the review. Nothing was duplicated; try again.')
    } finally {
      setStartingReview(false)
    }
  }

  return (
    <div ref={cardRef} className="contrib-card">
      {/* The quiet status row also carries dismissal, so the swipe has a visible,
          pointer- and keyboard-reachable equivalent. */}
      <div className="contrib-card__topline">
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

      {(record.repo || diffStat) ? (
        <p className="contrib-card__meta">
          {record.repo}
          {record.repo && diffStat ? <span> · </span> : null}
          {diffStat ? <span>{diffStat}</span> : null}
        </p>
      ) : null}

      <p className={failure ? 'contrib-card__error' : 'contrib-card__payoff'}>
        {failure?.message || (submitting
          ? 'Contribute is sending this now; its live status is attached to the review.'
          : blocker || 'The exact reviewed change is ready for your approval.')}
      </p>
      {failure?.detail && (
        <details className="contrib-card__failure-detail">
          <summary>Technical details</summary>
          <pre className="contrib-card__body">{failure.detail}</pre>
        </details>
      )}

      <div className="contrib-card__actions">
        {failure ? (
          <>
            {resolvedReview?.chatId ? (
              <button
                type="button"
                className="contrib-card__send"
                disabled={typeof onOpenChat !== 'function'}
                onClick={() => onOpenChat?.(resolvedReview.chatId)}
              >
                {['running', 'waiting', 'paused'].includes(resolvedReview.phase)
                  ? 'Review in progress'
                  : 'Open review conversation'}
              </button>
            ) : (
              <button
                type="button"
                className="contrib-card__send"
                disabled={!appToken || !recovery || startingReview || checkingReview}
                aria-busy={startingReview || checkingReview}
                onClick={startReview}
              >
                {startingReview
                  ? 'Starting review…'
                  : checkingReview
                    ? 'Checking review…'
                    : 'Fix and review'}
              </button>
            )}
            {!resolvedReview?.chatId ? (
              <button
                type="button"
                className="contrib-card__review"
                disabled={!onOpenContribute || !intent || startingReview}
                onClick={() => onOpenContribute?.(intent)}
              >
                Review in Contribute
              </button>
            ) : null}
          </>
        ) : busy ? (
          <button
            type="button"
            className="contrib-card__send"
            disabled
            aria-busy="true"
          >
            {action.busyLabel}
          </button>
        ) : !blocker ? (
          <>
            <button
              type="button"
              className="contrib-card__send"
              onClick={publish}
            >
              {action.label}
            </button>
            <button
              type="button"
              className="contrib-card__review"
              disabled={!onOpenContribute || !intent}
              onClick={() => onOpenContribute?.(intent)}
            >
              Review
            </button>
          </>
        ) : (
          <button
            type="button"
            className="contrib-card__send"
            disabled={!onOpenContribute || !intent}
            onClick={() => onOpenContribute?.(intent)}
          >
            {reviewDestinationLabel(record)}
          </button>
        )}
      </div>
      {busy ? (
        <p className="contrib-card__progress" role="status" aria-live="polite">
          {action.progress}
        </p>
      ) : null}
      {reviewStartError ? (
        <p className="contrib-card__error" role="status">{reviewStartError}</p>
      ) : null}
    </div>
  )
}
