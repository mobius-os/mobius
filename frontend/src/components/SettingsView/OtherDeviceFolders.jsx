/* Folders shared from your other browsers, one card per browser, each pausable from here. */
import StatusDot from '../ui/StatusDot.jsx'
import SharedFolderDeviceCard from './SharedFolderDeviceCard.jsx'
import { folderCount, otherFolderStatus, rightsLabel } from './sharedFolderLabels.js'
import '../ProviderAuth/ProviderAuth.css'

export default function OtherDeviceFolders({ devices, onPause }) {
  return devices.map(device => <OtherDevice key={device.id} device={device} onPause={onPause} />)
}

function OtherDevice({ device, onPause }) {
  const pausable = device.folders.filter(folder => otherFolderStatus(folder).usable)
  const pauseAll = pausable.length > 1 && (
    <button
      type="button"
      className="settings__btn settings__btn--outline settings__btn--sm"
      aria-label={`Pause all folders on ${device.label}`}
      onClick={() => { void onPause(pausable.map(folder => folder.id)) }}
    >
      Pause all
    </button>
  )
  return (
    <SharedFolderDeviceCard
      label={device.label}
      offline={!device.online}
      detail={`${folderCount(device.folders.length)} · ${device.online ? 'Online now' : 'Möbius is closed there'}`}
      action={pauseAll}
    >
      <div className="shared-folders__device-folders">
        {device.folders.map(folder => (
          <OtherFolderRow key={folder.id} device={device} folder={folder} onPause={onPause} />
        ))}
      </div>
      {!device.online && <p className="shared-folders__device-note">Open Möbius there to let agents in.</p>}
    </SharedFolderDeviceCard>
  )
}

function OtherFolderRow({ device, folder, onPause }) {
  const shown = otherFolderStatus(folder)
  return (
    <div className="provider-row shared-folders__other">
      <div className="provider-row__main">
        <span className="provider-row__info">
          <span className="provider-row__name-line">
            <span className="provider-row__name">{folder.name}</span>
          </span>
          <span className="provider-row__sub">{rightsLabel(folder)}</span>
          <StatusDot color={shown.color}>{shown.label}</StatusDot>
        </span>
        {shown.usable && (
          <button
            type="button"
            className="settings__btn settings__btn--outline settings__btn--sm shared-folders__other-pause"
            aria-label={`Pause ${folder.name} on ${device.label}`}
            onClick={() => { void onPause([folder.id]) }}
          >
            Pause
          </button>
        )}
      </div>
    </div>
  )
}
