/* Folders shared from the owner's other browsers, fetched fresh whenever the owner looks. */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client.js'
import { deviceId } from '../../lib/sharedFolders/folderStore.js'

async function fetchOtherFolders() {
  const response = await api.sharedFolders.list()
  if (!response.ok) return []
  const { folders = [] } = await response.json()
  const here = deviceId()
  return folders.filter(folder => folder.device?.id !== here)
}

// The last answer, kept for the page's lifetime: the row and the page it opens
// share it, so opening the page shows the folders at once and refreshes them
// quietly rather than flashing a loading state.
let lastAnswer = null

/** One entry per browser, in the order the server lists their folders. */
export function groupByDevice(folders) {
  const devices = folders.reduce((byId, folder) => {
    const id = folder.device?.id || 'unknown'
    const device = byId.get(id) || { id, label: folder.device?.label || 'Another browser', online: false, folders: [] }
    device.online ||= Boolean(folder.online)
    device.folders.push(folder)
    return byId.set(id, device)
  }, new Map())
  return [...devices.values()]
}

/**
 * `loaded` turns true once the first answer (or failure) has settled and stays
 * true, so a later refresh updates the list quietly instead of blanking it.
 *
 * Fetched when Settings shows it, when this browser's own folders change, and
 * when the owner comes back to the tab. Never on a timer: whether another
 * browser is online only matters while someone is looking.
 */
export default function useOtherDeviceFolders(localFolders) {
  const [folders, setFolders] = useState(lastAnswer ?? [])
  const [loaded, setLoaded] = useState(lastAnswer !== null)
  const [refreshes, setRefreshes] = useState(0)
  const refresh = useCallback(() => setRefreshes(count => count + 1), [])
  const localKey = localFolders.map(folder => `${folder.id}:${folder.state}`).join(',')

  useEffect(() => {
    let current = true
    fetchOtherFolders()
      .then(list => {
        lastAnswer = list
        if (current) setFolders(list)
      })
      .catch(() => {})
      .finally(() => { if (current) setLoaded(true) })
    return () => { current = false }
  }, [localKey, refreshes])

  useEffect(() => {
    const refreshWhenShown = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    document.addEventListener('visibilitychange', refreshWhenShown)
    return () => document.removeEventListener('visibilitychange', refreshWhenShown)
  }, [refresh])

  // Pausing works from here; resuming needs the device that shares the folder.
  const pause = useCallback(async (folderIds) => {
    await api.sharedFolders.pause(folderIds).catch(() => {})
    refresh()
  }, [refresh])

  const devices = useMemo(() => groupByDevice(folders), [folders])
  return { folders, devices, loaded, pause }
}
