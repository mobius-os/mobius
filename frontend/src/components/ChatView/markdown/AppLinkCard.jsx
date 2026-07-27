/* Render an internal app deep link as a compact preview that retains normal link fallback. */

import { ChevronRight } from 'lucide-react'
import { appQueries } from '../../../hooks/queries.js'
import AppLinkPreview from './AppLinkPreview.jsx'

export default function AppLinkCard({ card, onInternalNav }) {
  const appsQuery = appQueries.list.useQuery()
  const app = (appsQuery.data || []).find((candidate) => candidate.slug === card.app)
  const iconSrc = app?.id ? `/api/apps/${app.id}/icon?size=64` : card.iconSrc
  return (
    <a
      className="md-app-card"
      href={card.href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => {
        if (
          !onInternalNav
          || event.metaKey
          || event.ctrlKey
          || event.shiftKey
          || event.altKey
          || event.button !== 0
        ) return
        event.preventDefault()
        onInternalNav(new URL(card.href, window.location.href))
      }}
    >
      <AppLinkPreview appId={app?.id} card={card} fallbackIcon={iconSrc} />
      <span className="md-app-card__copy">
        <img className="md-app-card__app-icon" src={iconSrc} alt="" aria-hidden="true" />
        <span className="md-app-card__text">
          <span className="md-app-card__eyebrow">{card.appName} · {card.kind}</span>
          <strong>{card.title}</strong>
          <span>Open in {card.appName}</span>
        </span>
        <ChevronRight className="md-app-card__chevron" size={20} aria-hidden="true" />
      </span>
    </a>
  )
}
