/* Settings → Shared folders as one row, like an AI provider: a count and what needs you; the row opens the page. */
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
    <section className="settings-group shared-folders-summary" aria-labelledby="settings-shared-folders-title">
      <div className="settings-group__head">
        <div className="settings-group__heading">
          <h2 className="settings-group__title" id="settings-shared-folders-title">Shared folders</h2>
          <p className="settings-group__desc">Agents use files on your devices without uploading.</p>
        </div>
      </div>
      <div className="settings-list">
        <div className="settings-item shared-folders-summary__row shared-folders-skeleton" role="status" aria-busy="true" aria-label="Loading shared folders">
          <span className="settings-item__icon shared-folders-skeleton__block" aria-hidden="true" />
          <span className="shared-folders-skeleton__lines" aria-hidden="true">
            <span className="shared-folders-skeleton__block shared-folders-skeleton__line" style={{ width: '46%' }} />
            <span className="shared-folders-skeleton__block shared-folders-skeleton__line" style={{ width: '68%' }} />
          </span>
        </div>
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
    <section className="settings-group shared-folders-summary" aria-labelledby="settings-shared-folders-title">
      <div className="settings-group__head">
        <div className="settings-group__heading">
          <h2 className="settings-group__title" id="settings-shared-folders-title">Shared folders</h2>
          <p className="settings-group__desc">Agents use files on your devices without uploading.</p>
        </div>
      </div>
      <div className="settings-list">
        {/* Opens its own page, so no arrow: arrows in Settings mean "expands here". */}
        <button type="button" className="settings-item shared-folders-summary__row" onClick={onOpen} aria-label={`Shared folders: ${title}`}>
          <span className="settings-item__icon" aria-hidden="true"><Folder width={18} height={18} /></span>
          <span className="settings-item__body">
            <span className="settings-item__title">{title}</span>
            <span className="settings-item__sub">{detail}</span>
          </span>
          {needsYou > 0 && <span className="settings-badge settings-badge--accent">{needsYou} need{needsYou === 1 ? 's' : ''} you</span>}
        </button>
      </div>
    </section>
  )
}
