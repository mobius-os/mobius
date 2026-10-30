/* Settings → Shared folders page: one card per browser, this one first, so it is clear which device must be open. */
import { useRef, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { FolderPlus } from '@openai/apps-sdk-ui/components/Icon'
import useSharedFolders from '../../hooks/useSharedFolders.js'
import { addRememberedFolder, addVisitFolder } from '../../lib/sharedFolders/folderActions.js'
import { deviceLabel } from '../../lib/sharedFolders/folderStore.js'
import SharedFolderRow from './SharedFolderRow.jsx'
import SharedFolderDeviceCard from './SharedFolderDeviceCard.jsx'
import OtherDeviceFolders from './OtherDeviceFolders.jsx'
import ReadOnlyBrowserNote from './ReadOnlyBrowserNote.jsx'
import useOtherDeviceFolders from './useOtherDeviceFolders.js'
import { folderCount } from './sharedFolderLabels.js'
import './SharedFoldersSection.css'

const MODES = [
  { mode: 'read', title: 'Read only', detail: 'Agents can list and read files.' },
  {
    mode: 'edit',
    title: 'Read & edit',
    detail: 'Agents can also save, rename and move files. They never delete: unwanted and replaced files go to a “Möbius bin” folder. Hidden files, like .git or .env, stay read-only.',
  },
]

export default function SharedFoldersPage() {
  const state = useSharedFolders()
  const others = useOtherDeviceFolders(state.folders)
  const [notice, setNotice] = useState(null)
  const visitInput = useRef(null)
  const repickId = useRef(null)

  // A failure is shown as one; a done action may still say something (say, a folder shared Read only).
  const report = (outcome) => {
    if (outcome?.ok === false) setNotice(outcome.cancelled ? null : { color: 'danger', text: outcome.message })
    else setNotice(outcome?.notice ? { color: 'info', text: outcome.notice } : null)
  }
  const pickForVisit = (folderId = null) => {
    repickId.current = folderId
    visitInput.current?.click()
  }
  const onVisitFiles = async (event) => {
    const files = event.target.files
    report(await addVisitFolder(files, repickId.current))
    event.target.value = ''
  }

  return (
    <section className="shared-folders" aria-label="Shared folders">
      <p className="settings__subtext shared-folders__intro">
        Each browser shares its own folders. Agents reach a folder only while Möbius is open there.
      </p>
      <ThisBrowser state={state} report={report} pickForVisit={pickForVisit} />
      {notice && <Alert className="shared-folders__alert" color={notice.color} variant="soft" description={notice.text} />}
      {!others.loaded && <DeviceSkeleton />}
      {others.devices.length > 0 && (
        <div className="shared-folders__devices">
          <OtherDeviceFolders devices={others.devices} onPause={others.pause} />
        </div>
      )}
      <input
        ref={visitInput}
        className="shared-folders__visit-input"
        type="file"
        webkitdirectory=""
        multiple
        tabIndex={-1}
        aria-hidden="true"
        onChange={onVisitFiles}
      />
    </section>
  )
}

function ThisBrowser({ state, report, pickForVisit }) {
  if (state.status === 'loading') return <DeviceSkeleton />
  if (state.status === 'restart') {
    return <p className="settings__notice">Restart Möbius to turn on shared folders. Use Restart under Updates in Settings.</p>
  }
  if (state.status === 'unavailable') {
    return <p className="settings__notice">This browser cannot share folders. Use Chrome, Edge, Safari or Firefox.</p>
  }
  return (
    <>
      <SharedFolderDeviceCard label={deviceLabel()} current detail={`${folderCount(state.folders.length)} · ${thisBrowserRights(state)}`}>
        {state.folders.length > 0 ? (
          <div className="shared-folders__device-folders">
            {state.folders.map(folder => (
              <SharedFolderRow
                key={folder.id}
                folder={folder}
                report={report}
                onPickAgain={() => pickForVisit(folder.id)}
              />
            ))}
          </div>
        ) : (
          <p className="shared-folders__device-note">No folders shared from this browser yet.</p>
        )}
      </SharedFolderDeviceCard>
      <div className="shared-folders__adder">
        <FolderAdder state={state} report={report} pickForVisit={pickForVisit} />
      </div>
    </>
  )
}

/** A browser card's shape, empty: the page keeps its size while the folders load. */
function DeviceSkeleton() {
  return (
    <div className="shared-folders__device shared-folders-skeleton" role="status" aria-busy="true" aria-label="Loading folders">
      <div className="shared-folders__device-head" aria-hidden="true">
        <span className="shared-folders__device-icon shared-folders-skeleton__block" />
        <span className="shared-folders-skeleton__lines">
          <span className="shared-folders-skeleton__block shared-folders-skeleton__line" style={{ width: '40%' }} />
          <span className="shared-folders-skeleton__block shared-folders-skeleton__line" style={{ width: '58%' }} />
        </span>
      </div>
      <div className="shared-folders-skeleton__folder" aria-hidden="true">
        <span className="shared-folders-skeleton__block shared-folders-skeleton__line" style={{ width: '34%' }} />
        <span className="shared-folders-skeleton__block shared-folders-skeleton__line" style={{ width: '22%' }} />
      </div>
    </div>
  )
}

function thisBrowserRights(state) {
  if (state.canPickNative) return state.canEditNative ? 'Read & edit allowed' : 'Read only'
  return state.canRemember ? 'Read & edit allowed' : 'Read only, for this visit'
}

/** How this browser adds a folder: the phone app's picker, Chrome or Edge's remembered one, or a one-visit pick. */
function FolderAdder({ state, report, pickForVisit }) {
  if (state.canPickNative && !state.canEditNative) return <NativeFolderPicker report={report} />
  if (state.canPickNative) return <RememberedFolderPicker report={report} hint={NATIVE_HINT} />
  if (state.canRemember) return <RememberedFolderPicker report={report} />
  return <VisitFolderPicker onPick={() => pickForVisit()} />
}

/** Inside an older Möbius phone app: its own folder picker, read only (that app offers no writes). */
function NativeFolderPicker({ report }) {
  return (
    <div>
      <button
        type="button"
        className="settings__btn settings__btn--outline shared-folders__add"
        onClick={async () => report(await addRememberedFolder('read'))}
      >
        <FolderPlus width={16} height={16} aria-hidden="true" /> Add folder
      </button>
      <p className="shared-folders__hint">Agents can list and read its files. The app remembers the folder until you remove it.</p>
    </div>
  )
}

const NATIVE_HINT = 'The Möbius app remembers the folder until you remove it.'
const BROWSER_HINT = 'Pick a folder inside Downloads, Desktop or Documents. Browsers do not share those whole folders.'

function RememberedFolderPicker({ report, hint = BROWSER_HINT }) {
  const [choosing, setChoosing] = useState(false)
  if (!choosing) {
    return (
      <button type="button" className="settings__btn settings__btn--outline shared-folders__add" onClick={() => setChoosing(true)}>
        <FolderPlus width={16} height={16} aria-hidden="true" /> Add folder
      </button>
    )
  }
  // The picker must open straight from this tap, so each choice calls it directly.
  const pick = async (mode) => {
    const outcome = await addRememberedFolder(mode)
    report(outcome)
    if (!outcome.cancelled) setChoosing(false)
  }
  return (
    <div className="shared-folders__choices" role="group" aria-label="What can agents do in this folder?">
      <p className="shared-folders__choices-title">What can agents do in this folder?</p>
      {MODES.map(({ mode, title, detail }) => (
        <button key={mode} type="button" className="shared-folders__choice" onClick={() => { void pick(mode) }}>
          <span className="shared-folders__choice-title">{title}</span>
          <span className="shared-folders__choice-detail">{detail}</span>
        </button>
      ))}
      <p className="shared-folders__hint">{hint}</p>
      <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => setChoosing(false)}>
        Cancel
      </button>
    </div>
  )
}

function VisitFolderPicker({ onPick }) {
  return (
    <div className="shared-folders__visit">
      <button type="button" className="settings__btn settings__btn--outline shared-folders__add" onClick={onPick}>
        <FolderPlus width={16} height={16} aria-hidden="true" /> Pick a folder
      </button>
      <ReadOnlyBrowserNote />
    </div>
  )
}
