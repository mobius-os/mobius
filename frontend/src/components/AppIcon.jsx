/* AppIcon renders one installed app's artwork with a stable initials fallback. */
import { useRef, useState } from 'react'
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
  const iconOwner = item?.id ?? item?.slug ?? label
  const currentIconRef = useRef(null)
  currentIconRef.current = { owner: iconOwner, url: iconUrl }
  const [displayedIcon, setDisplayedIcon] = useState(() => ({
    owner: iconOwner,
    url: appIconIsReady(iconUrl) ? iconUrl : null,
  }))
  const displayedUrl = iconUrl && displayedIcon.owner === iconOwner
    ? displayedIcon.url
    : null
  // Keep the painted node mounted while the next version loads. URL keys let
  // React promote that same decoded <img> on load instead of remounting it.
  const candidateUrl = iconUrl && iconUrl !== displayedUrl ? iconUrl : null
  const imageUrls = [displayedUrl, candidateUrl].filter(Boolean)

  return (
    <span
      className={`app-icon${className ? ` ${className}` : ''}${displayedUrl ? ' is-image' : ''}`}
      style={{ '--app-color': item?.background_color || item?.theme_color || 'var(--accent)' }}
      aria-hidden="true"
    >
      <span>{appInitials(label)}</span>
      {imageUrls.map(url => (
        <img
          key={url}
          src={url}
          alt=""
          className={url === displayedUrl ? 'app-icon__image--displayed' : ''}
          loading="eager"
          decoding="async"
          onLoad={() => {
            rememberAppIconReady(url)
            const current = currentIconRef.current
            if (current.owner === iconOwner && current.url === url) {
              setDisplayedIcon(current)
            }
          }}
          onError={() => {
            forgetAppIconReady(url)
            if (url === displayedUrl) {
              setDisplayedIcon(current => (
                current.owner === iconOwner && current.url === url
                  ? { owner: iconOwner, url: null }
                  : current
              ))
            }
          }}
        />
      ))}
    </span>
  )
}
