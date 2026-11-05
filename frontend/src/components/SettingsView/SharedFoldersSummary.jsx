/* Settings → Shared folders as one row, like an AI provider: a count, what needs you, and › to the page. */
import { Folder } from '@openai/apps-sdk-ui/components/Icon'
import { isSharedBrowserAuth } from '../../api/client.js'
import useSharedFolders from '../../hooks/useSharedFolders.js'
import useOtherDeviceFolders from './useOtherDeviceFolders.js'
import { folderCount, needsYouCount } from './sharedFolderLabels.js'
import './SharedFoldersSection.css'

function summaryOf(state, devices) {
  if (state.status === 'restart') {
    return { title: 'Shared folders are off', detail: 'Restart Möbius to turn them on' }
  }
  const here = state.folders.length
  const elsewhere = devices.reduce((total, device) => total + device.folders.length, 0)
  if (here + elsewhere === 0) {
    return { title: 'No folders shared', detail: 'Let agents use files on your devices' }
  }
  const browsers = devices.length + (here ? 1 : 0)
  return {
    title: `${folderCount(here + elsewhere)} on ${browsers} browser${browsers === 1 ? '' : 's'}`,
    detail: `${here} on this browser · ${elsewhere} on other browsers`,
  }
}

function SummarySkeleton() {
  return (
    <section className="settings__section shared-folders-summary" aria-labelledby="settings-shared-folders-title">
      <h2 className="settings__section-title" id="settings-shared-folders-title">Shared folders</h2>
      <p className="settings__subtext settings__subtext--tight">Agents use files on your devices without uploading.</p>
      <div className="settings__account-link shared-folders-summary__row shared-folders-skeleton" role="status" aria-busy="true" aria-label="Loading shared folders">
        <span className="shared-folders-summary__icon shared-folders-skeleton__block" aria-hidden="true" />
        <span className="shared-folders-skeleton__lines" aria-hidden="true">
          <span className="shared-folders-skeleton__block shared-folders-skeleton__line" style={{ width: '46%' }} />
          <span className="shared-folders-skeleton__block shared-folders-skeleton__line" style={{ width: '68%' }} />
        </span>
      </div>
    </section>
  )
}

export default function SharedFoldersSummary({ onOpen }) {
  if (isSharedBrowserAuth()) return null
  return <Summary onOpen={onOpen} />
}

function Summary({ onOpen }) {
  const state = useSharedFolders()
  const { devices, loaded } = useOtherDeviceFolders(state.folders)
  const needsYou = needsYouCount(state.folders)
  // The counts come from this browser and from the server; say nothing until
  // both have answered, so the text appears once, final, instead of recounting.
  if (state.status === 'loading' || !loaded) return <SummarySkeleton />
  const { title, detail } = summaryOf(state, devices)
  return (
    <section className="settings__section shared-folders-summary" aria-labelledby="settings-shared-folders-title">
      <h2 className="settings__section-title" id="settings-shared-folders-title">Shared folders</h2>
      <p className="settings__subtext settings__subtext--tight">Agents use files on your devices without uploading.</p>
      <button type="button" className="settings__account-link shared-folders-summary__row" onClick={onOpen} aria-label={`Shared folders: ${title}`}>
        <span className="shared-folders-summary__icon" aria-hidden="true"><Folder width={18} height={18} /></span>
        <span className="settings__account-link-copy"><strong>{title}</strong><small>{detail}</small></span>
        {needsYou > 0 && <span className="shared-folders-summary__attention">{needsYou} need{needsYou === 1 ? 's' : ''} you</span>}
        <span className="settings__account-link-arrow" aria-hidden="true">›</span>
      </button>
    </section>
  )
}
