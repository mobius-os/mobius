import { useState, useEffect } from 'react'
import { apiFetch } from '../../api/client.js'
import './AppCanvas.css'

// version: bumped by Shell when an app_updated event arrives for this
// app.  Appended as ?v= to the iframe src to bust the browser cache
// and force a reload of the frame HTML (which includes theme CSS).
export default function AppCanvas({ appId, version = 0 }) {
  const [token, setToken] = useState(null)

  useEffect(() => {
    if (!appId) return
    let cancelled = false
    apiFetch('/auth/app-token', {
      method: 'POST',
      body: JSON.stringify({ app_id: appId }),
    })
      .then(r => r.json())
      .then(data => { if (!cancelled) setToken(data.token) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [appId, version])

  if (!appId) {
    return (
      <div className="canvas canvas--empty">
        <p className="canvas__hint">
          Open the menu to switch apps, or chat to create one.
        </p>
      </div>
    )
  }

  if (!token) return null

  const src = `/api/apps/${appId}/frame?token=${encodeURIComponent(token)}&v=${version}`

  return (
    <iframe
      key={`${appId}-${version}-${token}`}
      className="canvas"
      src={src}
      title="Mini-app"
      sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-top-navigation-by-user-activation"
      allow="microphone"
    />
  )
}
