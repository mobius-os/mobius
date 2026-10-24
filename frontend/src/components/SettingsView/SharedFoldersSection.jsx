/* Settings → Shared folders: folders on this device that your agents can work in, without uploading. */
import { useRef, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { FolderPlus } from '@openai/apps-sdk-ui/components/Icon'
import { isSharedBrowserAuth } from '../../api/client.js'
import useSharedFolders from '../../hooks/useSharedFolders.js'
import { addRememberedFolder, addVisitFolder } from '../../lib/sharedFolders/folderActions.js'
import SharedFolderRow from './SharedFolderRow.jsx'
import OtherDeviceFolders from './OtherDeviceFolders.jsx'
import ReadOnlyBrowserNote from './ReadOnlyBrowserNote.jsx'
import './SharedFoldersSection.css'

const MODES = [
  { mode: 'read', title: 'Read only', detail: 'Agents can list and read files.' },
  {
    mode: 'edit',
    title: 'Read & edit',
    detail: 'Agents can also save, rename and move files. They never delete: unwanted and replaced files go to a “Möbius bin” folder. Hidden files, like .git or .env, stay read-only.',
  },
]

export default function SharedFoldersSection() {
  const state = useSharedFolders()
  const [notice, setNotice] = useState(null)
  const visitInput = useRef(null)
  const repickId = useRef(null)
  if (isSharedBrowserAuth()) return null

  const report = (outcome) => setNotice(outcome?.ok === false && !outcome.cancelled ? outcome.message : null)
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
    <section className="settings__section shared-folders" aria-labelledby="settings-shared-folders-title">
      <h2 className="settings__section-title" id="settings-shared-folders-title">Shared folders</h2>
      <p className="settings__subtext shared-folders__intro">Agents use files without uploading.</p>
      <SectionBody state={state} report={report} pickForVisit={pickForVisit} />
      {notice && <Alert className="shared-folders__alert" color="danger" variant="soft" description={notice} />}
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

function SectionBody({ state, report, pickForVisit }) {
  if (state.status === 'loading') return <p className="settings__subtext">Loading…</p>
  if (state.status === 'restart') {
    return <p className="settings__notice">Restart Möbius to turn on shared folders. Use Restart under Updates below.</p>
  }
  if (state.status === 'unavailable') {
    return <p className="settings__notice">This browser cannot share folders. Use Chrome, Edge, Safari or Firefox.</p>
  }
  return (
    <>
      {state.folders.length > 0 && (
        <div className="settings__providers shared-folders__list">
          {state.folders.map(folder => (
            <SharedFolderRow
              key={folder.id}
              folder={folder}
              report={report}
              onPickAgain={() => pickForVisit(folder.id)}
            />
          ))}
        </div>
      )}
      {state.canRemember
        ? <RememberedFolderPicker report={report} />
        : <VisitFolderPicker onPick={() => pickForVisit()} />}
      <OtherDeviceFolders localFolders={state.folders} />
    </>
  )
}

function RememberedFolderPicker({ report }) {
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
      <p className="shared-folders__hint">
        Pick a folder inside Downloads, Desktop or Documents. Browsers do not share those whole folders.
      </p>
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
