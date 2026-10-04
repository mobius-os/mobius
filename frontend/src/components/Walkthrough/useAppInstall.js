/* Install for the guide's app cards. Every app goes through the same two steps: its access is
   checked (read-only), the owner reviews it in the confirmation, and Confirm and install installs
   exactly what was reviewed, bound to its digest as in App Store. The shell refreshes its own app
   list after an install, so the card turns to Installed on its own. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { capabilityRows, installReviewedApp, previewAppAccess } from './walkthroughAccess.js'

export function useAppInstall(catalog) {
  const [status, setStatus] = useState({})
  const [confirmation, setConfirmation] = useState(null)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const setOne = useCallback((id, value) => {
    if (!mounted.current) return
    setStatus(current => {
      const next = { ...current }
      if (value) next[id] = value
      else delete next[id]
      return next
    })
  }, [])

  const install = useCallback(async (id, item, digest) => {
    setOne(id, { state: 'installing' })
    try {
      const result = await installReviewedApp(item.manifest_url, digest)
      if (result.status === 'changed') {
        setOne(id, null)
        if (mounted.current) {
          setConfirmation({ id, item, rows: capabilityRows(result.preview.capability_contract), digest: result.preview.capability_digest, notice: 'The publisher changed this app’s access after you started. Nothing was installed. Check the access below.' })
        }
        return
      }
      setOne(id, { state: 'installed' })
    } catch (error) {
      setOne(id, { state: 'error', error: error.message || 'The app could not be installed.' })
    }
  }, [setOne])

  const begin = useCallback(async id => {
    const item = catalog?.get(id)
    if (!item?.manifest_url) return
    setOne(id, { state: 'checking' })
    try {
      const preview = await previewAppAccess(item.manifest_url)
      const rows = capabilityRows(preview.capability_contract)
      setOne(id, null)
      if (mounted.current) setConfirmation({ id, item, rows, digest: preview.capability_digest, notice: '' })
    } catch (error) {
      setOne(id, { state: 'error', error: error.message || 'This app’s access could not be checked right now.' })
    }
  }, [catalog, setOne])

  const approve = useCallback(() => {
    if (!confirmation) return
    const { id, item, digest } = confirmation
    setConfirmation(null)
    void install(id, item, digest)
  }, [confirmation, install])

  const dismiss = useCallback(() => setConfirmation(null), [])
  const statusOf = useCallback(id => status[id] || null, [status])
  return { statusOf, begin, confirmation, approve, dismiss }
}
