/* One browser in Settings → Shared folders: which device it is, then the folders it shares. */
import { Desktop, Mobile } from '@openai/apps-sdk-ui/components/Icon'

// Labels come from folderStore.deviceLabel(): "Chrome on Android", "Safari on iPhone".
const PHONE_LABEL = / on (iPhone|Android)$/

export default function SharedFolderDeviceCard({ label, current = false, offline = false, detail, action, children }) {
  const Icon = PHONE_LABEL.test(label) ? Mobile : Desktop
  return (
    <div className={`shared-folders__device${offline ? ' shared-folders__device--offline' : ''}`} role="group" aria-label={label}>
      <div className="shared-folders__device-head">
        <span className="shared-folders__device-icon" aria-hidden="true"><Icon width={18} height={18} /></span>
        <span className="shared-folders__device-copy">
          <span className="shared-folders__device-name">
            <span className="shared-folders__device-label">{label}</span>
            {current && <span className="shared-folders__device-chip">This browser</span>}
          </span>
          <span className="shared-folders__device-detail">{detail}</span>
        </span>
        {action}
      </div>
      {children}
    </div>
  )
}
