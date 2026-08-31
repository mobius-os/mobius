import { Agent, Bell, Chat, Grid, SettingsSlider } from '@openai/apps-sdk-ui/components/Icon'
import { useEffect, useRef, useState } from 'react'
import { notificationQueries } from '../../hooks/queries.js'
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
  updateAvailable = false,
  onUpdateNow,
  onUpdateLater,
}) {
  const { data, isLoading, isError } = notificationQueries.list.useQuery({ enabled: active })
  const rows = data ?? []
  const [now, setNow] = useState(() => Date.now())
  const pointerSelectionRef = useRef(null)
  const [isClearing, setIsClearing] = useState(false)
  const [clearError, setClearError] = useState(false)

  // Relative labels are live information, not a one-time formatting pass.
  // Refreshing once a minute keeps an open preview from saying "now" forever.
  useEffect(() => {
    if (!active) return undefined
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => window.clearInterval(timer)
  }, [active])

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
      <div className="notifications__content">
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
                    A Möbius update is ready.
                  </span>
                  <span className="notifications__row-body">
                    Refresh to use the latest changes.
                  </span>
                  <span className="notifications__update-actions">
                    <button
                      type="button"
                      className="notifications__update-action notifications__update-action--primary"
                      onClick={onUpdateNow}
                    >
                      Update now
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
            const nav = parseNotificationTarget(n.target)
            const Icon = ICONS[iconKindForSource(n.source_type)] ?? ICONS.default
            const body = (
              <>
                <span className="notifications__row-icon" aria-hidden="true">
                  <Icon width={17} height={17} />
                </span>
                <span className="notifications__row-main">
                  <span className="notifications__row-title">{n.title}</span>
                  {n.body ? (
                    <span className="notifications__row-body">{n.body}</span>
                  ) : null}
                </span>
                <time
                  className="notifications__row-time"
                  dateTime={n.sent_at}
                >
                  {formatRelativeTime(n.sent_at, now)}
                </time>
              </>
            )
            return (
              <li key={n.id} className="notifications__row-item">
                {nav ? (
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
              </li>
            )
          })}
        </ul>
      </div>
    </section>
  )
}
