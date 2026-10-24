/* Folders shared from your other browsers and devices: everything agents can reach, each pausable from here. */
import { useEffect, useState } from 'react'
import { api } from '../../api/client.js'
import { deviceId } from '../../lib/sharedFolders/folderStore.js'
import StatusDot from '../ui/StatusDot.jsx'
import '../ProviderAuth/ProviderAuth.css'

function rightsLabel(folder) {
  return folder.mode === 'edit' && folder.access === 'remembered' ? 'Read & edit' : 'Read only'
}

async function fetchOtherFolders() {
  const response = await api.sharedFolders.list()
  if (!response.ok) return []
  const { folders = [] } = await response.json()
  const here = deviceId()
  return folders.filter(folder => folder.device?.id !== here)
}

function status(folder) {
  if (!folder.online) return { color: '--muted', label: 'Offline' }
  if (folder.state === 'paused') return { color: '--muted', label: 'Paused' }
  return { color: '--green', label: 'Online' }
}

export default function OtherDeviceFolders({ localFolders }) {
  const [folders, setFolders] = useState([])
  const [refreshes, setRefreshes] = useState(0)
  // Refresh whenever this browser's own list changes (a cheap, owner-paced moment).
  const localKey = localFolders.map(folder => `${folder.id}:${folder.state}`).join(',')
  useEffect(() => {
    let current = true
    fetchOtherFolders()
      .then(list => { if (current) setFolders(list) })
      .catch(() => {})
    return () => { current = false }
  }, [localKey, refreshes])
  // Pausing works from here; resuming needs the device that shares the folder.
  const pause = async (id) => {
    await api.sharedFolders.pause([id]).catch(() => {})
    setRefreshes(count => count + 1)
  }
  if (!folders.length) return null
  return (
    <div className="shared-folders__others">
      <p className="shared-folders__others-title">Shared from your other browsers</p>
      <div className="settings__providers shared-folders__others-list">
        {folders.map(folder => {
          const shown = status(folder)
          return (
            <div key={folder.id} className="provider-row shared-folders__other">
              <div className="provider-row__main">
                <span className="provider-row__info">
                  <span className="provider-row__name-line">
                    <span className="provider-row__name">{folder.name}</span>
                  </span>
                  <span className="provider-row__sub">{folder.device?.label} · {rightsLabel(folder)}</span>
                  <StatusDot color={shown.color}>{shown.label}</StatusDot>
                </span>
                {folder.online && folder.state === 'ready' && (
                  <button
                    type="button"
                    className="settings__btn settings__btn--outline settings__btn--sm shared-folders__other-pause"
                    aria-label={`Pause ${folder.name} on ${folder.device?.label}`}
                    onClick={() => { void pause(folder.id) }}
                  >
                    Pause
                  </button>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
