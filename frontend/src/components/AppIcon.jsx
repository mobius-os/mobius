/* AppIcon renders one installed app's artwork with a stable initials fallback. */
import { useState } from 'react'
import {
  appIconIsReady,
  appIconUrl,
  appInitials,
  forgetAppIconReady,
  rememberAppIconReady,
} from './appIcon.js'
import './AppIcon.css'

export default function AppIcon({ item, label, className = '' }) {
  const iconUrl = appIconUrl(item)
  const [loadedUrl, setLoadedUrl] = useState(
    () => (appIconIsReady(iconUrl) ? iconUrl : null),
  )
  const hasImage = Boolean(
    iconUrl && (loadedUrl === iconUrl || appIconIsReady(iconUrl)),
  )

  return (
    <span
      className={`app-icon${className ? ` ${className}` : ''}${hasImage ? ' is-image' : ''}`}
      style={{ '--app-color': item?.background_color || item?.theme_color || 'var(--accent)' }}
      aria-hidden="true"
    >
      <span>{appInitials(label)}</span>
      {iconUrl && (
        <img
          src={iconUrl}
          alt=""
          loading="eager"
          decoding="async"
          onLoad={event => {
            event.currentTarget.hidden = false
            rememberAppIconReady(iconUrl)
            setLoadedUrl(iconUrl)
          }}
          onError={event => {
            event.currentTarget.hidden = true
            forgetAppIconReady(iconUrl)
            setLoadedUrl(null)
          }}
        />
      )}
    </span>
  )
}
