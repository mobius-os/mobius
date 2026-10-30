/* Install for the guide's app cards: one tap installs the app. The shell refreshes its own app list
   after an install, so the card turns to Installed on its own. An app asks what it may do the first
   time it opens, not here. Each app tracks its own install, so several can run at once. */
import { useCallback, useRef, useState } from 'react'
import { apiFetch } from '../../api/client.js'
import { detailToMessage } from '../../lib/errorDetail.js'

export function useAppInstall(catalog) {
  const [status, setStatus] = useState({})
  const running = useRef(new Set())

  const install = useCallback(async id => {
    const item = catalog?.get(id)
    if (!item?.manifest_url || running.current.has(id)) return
    running.current.add(id)
    setStatus(current => ({ ...current, [id]: { state: 'installing' } }))
    let next
    try {
      const response = await apiFetch('/apps/install', { method: 'POST', body: JSON.stringify({ manifest_url: item.manifest_url }), timeoutMs: 120_000 })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(detailToMessage(data.detail, 'The app could not be installed.'))
      next = { state: 'installed', warnings: Array.isArray(data.warnings) ? data.warnings : [] }
    } catch (error) {
      next = { state: 'error', error: error.message || 'The app could not be installed.' }
    }
    running.current.delete(id)
    setStatus(current => ({ ...current, [id]: next }))
  }, [catalog])

  const statusOf = useCallback(id => status[id] || null, [status])
  return { statusOf, install }
}
