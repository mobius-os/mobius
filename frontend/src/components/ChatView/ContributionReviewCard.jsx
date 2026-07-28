import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import X from 'lucide-react/dist/esm/icons/x.mjs'
import { api } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
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
  sendBlocker,
  statusLabel,
  visibleReviewItems,
} from './contributionReviewModel.js'
import './ContributionReviewCard.css'

// How long the post-send acknowledgement stays before clearing itself. Long
// enough to read and tap through to GitHub, short enough that walking away never
// leaves a stale box wedged above the composer.
const SENT_VISIBLE_MS = 12000

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

  const [busyId, setBusyId] = useState(null)
  const [error, setError] = useState(null)
  const [sentRows, setSentRows] = useState([])
  // State disables the buttons on the next render; the ref closes the smaller
  // same-frame window too. One owner press can therefore claim exactly one
  // record, even if another click lands before React has painted the lock.
  const activeSendRef = useRef(null)
  // Dismissals are persisted, so this only forces the re-render; the stored
  // decision is what actually filters the list.
  const [dismissRevision, setDismissRevision] = useState(0)

  const storage = typeof localStorage !== 'undefined' ? localStorage : null
  const sentIds = new Set(sentRows.map(row => row.id))
  // Remove a successful single record locally before the ledger refetch
  // returns. Stack review items never send from chat and stay untouched.
  const pendingItems = visibleReviewItems(data, storage).filter(
    item => item.kind !== 'record' || !sentIds.has(item.record.id),
  )
  const panel = reviewPanelSummary(pendingItems.length, sentRows.length)
  const grouped = panel.count > 1
  void dismissRevision
  if (!appId) return null
  if (panel.count === 0) return null

  async function send(record) {
    if (activeSendRef.current !== null) return
    activeSendRef.current = record.id
    setBusyId(record.id)
    setError(null)
    try {
      const res = await api.contributions.submit(appId, record.id, {
        autopilot: autopilotOnSend(data),
      })
      const body = await res.json().catch(() => null)
      if (!res.ok) {
        const detail = body?.detail?.message || body?.detail
        setError({
          id: record.id,
          message: typeof detail === 'string'
            ? detail
            : 'Could not contribute this. Open Contribute for the details.',
        })
        return
      }
      const sent = {
        id: record.id,
        number: body?.number ?? body?.record?.number ?? null,
        url: body?.url || body?.record?.url || null,
        repo: record.repo,
      }
      setSentRows(rows => [
        ...rows.filter(row => row.id !== sent.id),
        sent,
      ])
    } catch {
      setError({
        id: record.id,
        message: 'Could not reach the server. Nothing was contributed.',
      })
    } finally {
      if (activeSendRef.current === record.id) activeSendRef.current = null
      setBusyId(current => current === record.id ? null : current)
      queryClient.invalidateQueries({ queryKey, exact: true })
    }
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
      {sentRows.map(sent => (
        <SentRow
          key={`sent:${sent.id}`}
          sent={sent}
          onDismiss={() => setSentRows(rows => (
            rows.filter(row => row.id !== sent.id)
          ))}
        />
      ))}
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
            connected={data?.connected !== false}
            autopilot={autopilotOnSend(data)}
            busy={busyId === record.id}
            locked={busyId !== null}
            error={error?.id === record.id ? error.message : null}
            onSend={send}
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
 * It lives as a hook rather than inside one card because EVERY card here needs an
 * exit — the confirmation shipped without one, and an undismissable box above the
 * composer is worse than no confirmation at all.
 */
function useSwipeToDismiss(onDismiss) {
  const cardRef = useRef(null)
  const swipe = useRef({ x: 0, y: 0, active: false, claimed: false })
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
      el.style.transform = `translateX(${dx}px)`
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


/**
 * The acknowledgement after this card's own Send.
 *
 * It is an acknowledgement, NOT a permanent record — the Contribute app owns the
 * history and the chat reply carries the link. So it gets every exit the other
 * cards have (swipe, the band control) AND clears itself, because the first
 * version shipped with no exit at all and became an undismissable box above the
 * composer.
 */
function SentRow({ sent, onDismiss }) {
  const cardRef = useSwipeToDismiss(onDismiss)
  const dismissRef = useRef(onDismiss)
  dismissRef.current = onDismiss

  useEffect(() => {
    const timer = window.setTimeout(() => dismissRef.current?.(), SENT_VISIBLE_MS)
    return () => window.clearTimeout(timer)
  }, [])

  return (
    <div ref={cardRef} className="contrib-card contrib-card--sent" role="status">
      <div className="contrib-card__badge">
        <span>Contributed</span>
        <button
          type="button"
          className="contrib-card__dismiss"
          aria-label="Dismiss"
          onClick={() => onDismiss?.()}
        >
          <X size={14} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>
      <p className="contrib-card__summary">
        {sent.number
          ? `Pull request #${sent.number} is with the maintainers.`
          : 'It is with the maintainers now.'}
      </p>
      {sent.repo && <p className="contrib-card__meta">{sent.repo}</p>}
      {sent.url && (
        <div className="contrib-card__actions">
          <a
            className="contrib-card__link-btn"
            href={sent.url}
            target="_blank"
            rel="noreferrer noopener"
          >
            View on GitHub
          </a>
        </div>
      )}
    </div>
  )
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
          <X size={14} strokeWidth={2} aria-hidden="true" />
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
  record, connected, autopilot, busy, locked, error, onSend, onOpenContribute,
  onDismiss,
}) {
  const [open, setOpen] = useState(false)
  const blocker = sendBlocker(record, { connected })
  const diffStat = diffStatSummary(record.diff_stat)
  const submitting = record.status === 'submitting'
  const cardRef = useSwipeToDismiss(onDismiss)

  return (
    <div
      ref={cardRef}
      className={`contrib-card${blocker ? ' contrib-card--blocked' : ''}`}
    >
      {/* Header band, then what it is, then the actions. The band also carries
          dismissal, so the swipe has a visible, pointer- and keyboard-reachable
          equivalent rather than being a touch-only secret. */}
      <div className="contrib-card__badge">
        <span>{statusLabel(record, !!blocker)}</span>
        <button
          type="button"
          className="contrib-card__dismiss"
          aria-label="Dismiss — keeps it in Contribute"
          onClick={() => onDismiss?.()}
        >
          <X size={14} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>
      <p className="contrib-card__summary">
        {record.summary || record.title || 'An improvement is ready to contribute'}
      </p>

      <p className="contrib-card__meta">
        {record.repo}
        {diffStat ? <span> · {diffStat}</span> : null}
      </p>

      {/* The payoff, but never next to a problem: a blocked review needs the
          reason, not encouragement. */}
      {!blocker && !error && !submitting && (
        <p className="contrib-card__payoff">{payoffLine(record)}</p>
      )}
      {autopilot && !blocker && !error && !submitting && (
        <p className="contrib-card__meta">
          Möbius will also handle review feedback after you contribute.
        </p>
      )}
      {blocker && <p className="contrib-card__blocker">{blocker}</p>}
      {error && <p className="contrib-card__blocker">{error}</p>}

      <div className="contrib-card__actions">
        <button
          type="button"
          className="contrib-card__send"
          disabled={locked || submitting || !!blocker}
          onClick={() => onSend(record)}
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
        {/* Only when something needs sorting out — the happy path stays two
            buttons, and Contribute owns every state this card cannot fix. */}
        {(blocker || error) && onOpenContribute && (
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
