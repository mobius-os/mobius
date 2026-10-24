/* While an agent works in a shared folder on any device, show it and offer Stop (pauses every shared folder, everywhere). */
import { createPortal } from 'react-dom'
import { FolderShared } from '@openai/apps-sdk-ui/components/Icon'
import useSharedFolders from '../../hooks/useSharedFolders.js'
import { stopEverywhere } from '../../lib/sharedFolders/folderActions.js'

export default function SharedFolderActivity() {
  const { activity } = useSharedFolders()
  if (!activity || typeof document === 'undefined') return null
  const where = activity.deviceLabel ? `${activity.folderName} on ${activity.deviceLabel}` : activity.folderName
  return createPortal(
    <button
      type="button"
      className="screen-control-button screen-control-button--active shared-folder-activity"
      aria-label={`Agent is using ${where}. Stop sharing folders on every device.`}
      title="Pause every shared folder, on every device"
      onClick={() => { void stopEverywhere() }}
    >
      <FolderShared width={18} height={18} aria-hidden="true" />
      <span className="shared-folder-activity__label">
        <span className="shared-folder-activity__text">Agent is using {activity.folderName}</span>
        {activity.deviceLabel && <span className="shared-folder-activity__device">on {activity.deviceLabel}</span>}
      </span>
      <span className="screen-control-button__live" aria-hidden="true" />
      <span className="screen-control-button__stop">Stop</span>
    </button>,
    document.body,
  )
}
