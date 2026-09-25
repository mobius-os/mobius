import { Agent, Bell, Chat, Grid, SettingsSlider, X } from '@openai/apps-sdk-ui/components/Icon'
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { notificationQueries } from '../../hooks/queries.js'
import { formatDateTime } from '../../lib/dateTimeFormat.js'
import {
  completeNotificationRecovery,
  hasRecoveryReceipt,
  notificationRecoveryAction,
  recoveryFailure,
  recoveryUnavailableLabel,
} from '../../lib/notificationRecovery.js'
import { parseNotificationTarget } from '../../lib/notificationTarget.js'
import {
  pointerSelectionChangedWithin,
  textSelectionSnapshot,
} from '../../lib/selectableTextControl.js'
import { formatRelativeTime, iconKindForSource } from './notificationsModel.js'
import './NotificationsView.css'

const ICONS = {
  system: SettingsSlider,
  agent: Agent,
  chat: Chat,
  app: Grid,
  default: Bell,
}

// A deliberately small shell preview, not a new navigation world. TRUST:
// app-authored title/body stay plain text, app-authored icon URLs are ignored,
// and targets pass through the fail-closed shared parser before navigation.
export default function NotificationsView({
  active = false,
  onOpenTarget,
  onClearAll,
  onDismiss,
  onRecoveryAction,
  updateAvailable = false,
  onUpdateNow,
  onUpdateLater,
}) {
  const queryClient = useQueryClient()
  const {
    data, isLoading, isError, hasNextPage, fetchNextPage, isFetchingNextPage,
    isFetchNextPageError,
  } = notificationQueries.list.useQuery({ enabled: active })
  const rows = data?.pages.flat() ?? []
  const [now, setNow] = useState(() => Date.now())
  const pointerSelectionRef = useRef(null)
  const contentRef = useRef(null)
  const paginationRef = useRef(null)
  const [isClearing, setIsClearing] = useState(false)
  const [clearError, setClearError] = useState(false)
  const [dismissState, setDismissState] = useState({})
  const [recoveryState, setRecoveryState] = useState({})

  // Relative labels are live information, not a one-time formatting pass.
  // Refreshing once a minute keeps an open preview from saying "now" forever.
  useEffect(() => {
    if (!active) return undefined
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => window.clearInterval(timer)
  }, [active])

  useEffect(() => {
    if (
      !active || !hasNextPage || isFetchingNextPage || isFetchNextPageError
      || !contentRef.current || !paginationRef.current
      || typeof IntersectionObserver === 'undefined'
    ) return undefined
    const observer = new IntersectionObserver((entries) => {
      if (entries.some(entry => entry.isIntersecting)) void fetchNextPage()
    }, { root: contentRef.current, rootMargin: '120px 0px', threshold: 0 })
    observer.observe(paginationRef.current)
    return () => observer.disconnect()
  }, [active, fetchNextPage, hasNextPage, isFetchNextPageError, isFetchingNextPage])

  const handleClearAll = async () => {
    if (!rows.length || isClearing) return
    setIsClearing(true)
    setClearError(false)
    try {
      await onClearAll()
    } catch {
      setClearError(true)
    } finally {
      setIsClearing(false)
    }
  }

  const handleRecovery = async (notification, action) => {
    if (!onRecoveryAction || recoveryState[notification.id] === 'working') return
    if (recoveryUnavailableLabel(action)) {
      setNow(Date.now())
      return
    }
    setRecoveryState(current => ({ ...current, [notification.id]: 'working' }))
    try {
      const completed = await onRecoveryAction(notification.id, action)
      queryClient.setQueryData(notificationQueries.list.key, current => (
        completeNotificationRecovery(current, notification.id, completed.completedAt)
      ))
      setRecoveryState(current => ({ ...current, [notification.id]: 'done' }))
    } catch (error) {
      setRecoveryState(current => ({ ...current, [notification.id]: recoveryFailure(error) }))
    }
  }

  const handleDismiss = async (notificationId) => {
    if (!onDismiss || dismissState[notificationId] === 'working') return
    setDismissState(current => ({ ...current, [notificationId]: 'working' }))
    try {
      await onDismiss(notificationId)
    } catch {
      setDismissState(current => ({ ...current, [notificationId]: 'error' }))
    }
  }

  return (
    <section
      id="notification-preview"
      className="notifications"
      aria-labelledby="notification-preview-title"
    >
      <div className="notifications__header">
        <h2 id="notification-preview-title" className="notifications__title">
          Notifications
        </h2>
        {rows.length > 0 && (
          <button
            type="button"
            className="notifications__clear"
            onClick={handleClearAll}
            disabled={isClearing}
          >
            {isClearing ? 'Clearing…' : 'Clear all'}
          </button>
        )}
      </div>
      <div className="notifications__content" ref={contentRef}>
        {isLoading && (
          <p className="notifications__hint" role="status">Loading…</p>
        )}
        {isError && !rows.length && (
          <p className="notifications__hint" role="alert">
            Couldn’t load notifications. They’ll retry automatically.
          </p>
        )}
        {clearError && (
          <p className="notifications__hint notifications__hint--error" role="alert">
            Couldn’t clear notifications. Try again when you’re online.
          </p>
        )}
        {!isLoading && !isError && rows.length === 0 && !updateAvailable && (
          <div className="notifications__empty">
            <Bell width={28} height={28} aria-hidden="true" />
            <p>Updates from your apps and agents will appear here.</p>
          </div>
        )}
        <ul className="notifications__list">
          {updateAvailable && (
            <li className="notifications__row-item">
              <div className="notifications__row notifications__row--update">
                <span className="notifications__row-icon" aria-hidden="true">
                  <SettingsSlider width={17} height={17} />
                </span>
                <span className="notifications__row-main">
                  <span className="notifications__row-title">
                    New shell ready.
                  </span>
                  <span className="notifications__row-body">
                    Reload to use the latest interface changes.
                  </span>
                  <span className="notifications__update-actions">
                    <button
                      type="button"
                      className="notifications__update-action notifications__update-action--primary"
                      onClick={onUpdateNow}
                    >
                      Reload shell
                    </button>
                    <button
                      type="button"
                      className="notifications__update-action"
                      onClick={onUpdateLater}
                    >
                      Later
                    </button>
                  </span>
                </span>
              </div>
            </li>
          )}
          {rows.map((n) => {
            const parsedNav = parseNotificationTarget(n.target)
            const nav = parsedNav?.view === 'chat' && n.title === 'Möbius needs your answer'
              ? { ...parsedNav, focusQuestion: true }
              : parsedNav
            const recovery = notificationRecoveryAction(n)
            const protectsDismissal = hasRecoveryReceipt(n)
            const recoveryStatus = recoveryState[n.id]
            const unavailableLabel = recovery && (
              recoveryStatus === 'done' ? 'Restored' : (
                recoveryUnavailableLabel(recovery, now)
                || (recoveryStatus?.terminal ? recoveryStatus.message : null)
              )
            )
            const Icon = ICONS[iconKindForSource(n.source_type)] ?? ICONS.default
            const body = (
              <>
                <span className="notifications__row-icon" aria-hidden="true">
                  <Icon width={17} height={17} />
                </span>
                <span className="notifications__row-main">
                  <span className="notifications__row-head">
                    <span className="notifications__row-title">{n.title}</span>
                    <time
                      className="notifications__row-time"
                      dateTime={n.sent_at}
                    >
                      {formatRelativeTime(n.sent_at, now)}
                    </time>
                  </span>
                  {n.body ? (
                    <span className="notifications__row-body">{n.body}</span>
                  ) : null}
                  {recovery ? (
                    <span className="notifications__recovery">
                      {unavailableLabel ? (
                        <span className="notifications__recovery-status" role="status">
                          {unavailableLabel}
                        </span>
                      ) : (
                        <button
                          type="button"
                          className="notifications__recovery-action"
                          disabled={!onRecoveryAction || recoveryStatus === 'working'}
                          onClick={() => handleRecovery(n, recovery)}
                        >
                          {recoveryStatus === 'working' ? 'Restoring…' : recovery.title}
                        </button>
                      )}
                      {!unavailableLabel && (
                        <span className="notifications__recovery-deadline">
                          Expires: {formatDateTime(recovery.expiresAt)}
                        </span>
                      )}
                      {recoveryStatus?.message && !recoveryStatus.terminal && !unavailableLabel ? (
                        <span className="notifications__recovery-error" role="alert">
                          {recoveryStatus.message}
                        </span>
                      ) : null}
                    </span>
                  ) : null}
                </span>
              </>
            )
            return (
              <li key={n.id} className="notifications__row-item">
                <div className="notifications__row-shell">
                  {nav && !recovery ? (
                    <button
                      type="button"
                      className="notifications__row notifications__row--link"
                      onPointerDown={() => {
                        pointerSelectionRef.current = textSelectionSnapshot()
                      }}
                      onClick={(event) => {
                        const selectionBeforePointer = pointerSelectionRef.current
                        pointerSelectionRef.current = null
                        if (
                          event.detail !== 0
                          && pointerSelectionChangedWithin(
                            selectionBeforePointer,
                            event.currentTarget,
                          )
                        ) return
                        onOpenTarget?.(nav)
                      }}
                    >
                      {body}
                    </button>
                  ) : (
                    <div className="notifications__row">{body}</div>
                  )}
                  {!protectsDismissal && (
                    <button
                      type="button"
                      className="notifications__dismiss"
                      aria-label={`Dismiss ${n.title}`}
                      title="Dismiss notification"
                      disabled={!onDismiss || dismissState[n.id] === 'working'}
                      onClick={() => handleDismiss(n.id)}
                    >
                      <X width={16} height={16} aria-hidden="true" />
                    </button>
                  )}
                </div>
                {dismissState[n.id] === 'error' && (
                  <p className="notifications__dismiss-error" role="alert">
                    Couldn’t dismiss this notification. Try again.
                    <button type="button" onClick={() => handleDismiss(n.id)}>Try again</button>
                  </p>
                )}
              </li>
            )
          })}
        </ul>
        {hasNextPage && (
          <div className="notifications__pagination" ref={paginationRef}>
            {isFetchingNextPage && (
              <p className="notifications__hint" role="status">Loading older notifications…</p>
            )}
            {isFetchNextPageError && (
              <div className="notifications__pagination-error" role="alert">
                <span>Couldn’t load older notifications.</span>
                <button type="button" onClick={() => fetchNextPage()}>Try again</button>
              </div>
            )}
            {typeof IntersectionObserver === 'undefined' && !isFetchNextPageError && (
              <button type="button" className="notifications__load-more" onClick={() => fetchNextPage()}>
                Load older notifications
              </button>
            )}
          </div>
        )}
      </div>
    </section>
  )
}
