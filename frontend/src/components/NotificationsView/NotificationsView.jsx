import Bell from 'lucide-react/dist/esm/icons/bell.mjs'
import AppWindow from 'lucide-react/dist/esm/icons/app-window.mjs'
import BotMessageSquare from 'lucide-react/dist/esm/icons/bot-message-square.mjs'
import MessageSquare from 'lucide-react/dist/esm/icons/message-square.mjs'
import Settings2 from 'lucide-react/dist/esm/icons/settings-2.mjs'
import X from 'lucide-react/dist/esm/icons/x.mjs'
import { useEffect, useState } from 'react'
import { notificationQueries } from '../../hooks/queries.js'
import { parseNotificationTarget } from '../../lib/notificationTarget.js'
import { formatRelativeTime, iconKindForSource } from './notificationsModel.js'
import './NotificationsView.css'

const ICONS = {
  system: Settings2,
  agent: BotMessageSquare,
  chat: MessageSquare,
  app: AppWindow,
  default: Bell,
}

// A deliberately small shell preview, not a new navigation world. TRUST:
// app-authored title/body stay plain text, app-authored icon URLs are ignored,
// and targets pass through the fail-closed shared parser before navigation.
export default function NotificationsView({ active = false, onClose, onOpenTarget }) {
  const { data, isLoading, isError } = notificationQueries.list.useQuery({ enabled: active })
  const rows = data ?? []
  const [now, setNow] = useState(() => Date.now())

  // Relative labels are live information, not a one-time formatting pass.
  // Refreshing once a minute keeps an open preview from saying "now" forever.
  useEffect(() => {
    if (!active) return undefined
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => window.clearInterval(timer)
  }, [active])

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
        <button
          type="button"
          className="notifications__close"
          aria-label="Close notifications"
          onClick={onClose}
        >
          <X size={18} aria-hidden="true" />
        </button>
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
        {!isLoading && !isError && rows.length === 0 && (
          <div className="notifications__empty">
            <Bell size={28} aria-hidden="true" />
            <p>Updates from your apps and agents will appear here.</p>
          </div>
        )}
        <ul className="notifications__list">
          {rows.map((n) => {
            const nav = parseNotificationTarget(n.target)
            const Icon = ICONS[iconKindForSource(n.source_type)] ?? ICONS.default
            const body = (
              <>
                <span className="notifications__row-icon" aria-hidden="true">
                  <Icon size={17} />
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
                    onClick={() => onOpenTarget?.(nav)}
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
