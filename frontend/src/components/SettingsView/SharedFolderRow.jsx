/* One shared folder in Settings: its state, a one-tap fix when needed, and Manage (pause, remove). */
import { useState } from 'react'
import ProviderRow from '../ProviderAuth/ProviderRow.jsx'
import StatusDot from '../ui/StatusDot.jsx'
import {
  reconnectFolder, removeFolder, setFolderPaused,
} from '../../lib/sharedFolders/folderActions.js'

const STATUS = {
  ready: { color: '--green', label: 'Ready' },
  paused: { color: '--muted', label: 'Paused' },
  needs_permission: { color: '--accent', label: 'Needs your OK' },
  needs_pick: { color: '--accent', label: 'Pick again to share' },
}

function subtitle(folder) {
  const rights = folder.mode === 'edit' && folder.access === 'remembered' ? 'Read & edit' : 'Read only'
  const kept = folder.access === 'remembered' ? 'Remembered on this browser' : 'Until you leave Möbius'
  return `${rights} · ${kept}`
}

export default function SharedFolderRow({ folder, report, onPickAgain }) {
  const [open, setOpen] = useState(false)
  const status = STATUS[folder.state] || STATUS.paused
  return (
    <ProviderRow
      name={folder.name}
      connected
      subtitle={subtitle(folder)}
      statusNode={<StatusDot color={status.color}>{status.label}</StatusDot>}
      detailNode={<FolderPrompt folder={folder} report={report} onPickAgain={onPickAgain} />}
      actionLabel="Manage"
      expanded={open}
      onToggleExpand={() => setOpen(value => !value)}
    >
      <ManagePanel folder={folder} report={report} />
    </ProviderRow>
  )
}

/** The single action that makes an unready folder usable again. */
function FolderPrompt({ folder, report, onPickAgain }) {
  const prompts = {
    // requestPermission needs this tap's activation: call it directly.
    needs_permission: {
      text: 'The browser asks again after you close Möbius.',
      label: 'Reconnect',
      run: async () => report(await reconnectFolder(folder.id)),
    },
    needs_pick: { text: 'This browser forgets the folder when you leave.', label: 'Pick again', run: onPickAgain },
    paused: {
      text: 'Agents cannot use this folder now.',
      label: 'Resume',
      run: async () => report(await setFolderPaused(folder.id, false)),
    },
  }
  const prompt = prompts[folder.state]
  if (!prompt) return null
  return (
    <div className="shared-folders__prompt">
      <span className="shared-folders__prompt-text">{prompt.text}</span>
      <button type="button" className="settings__btn settings__btn--sm" onClick={() => { void prompt.run() }}>
        {prompt.label}
      </button>
    </div>
  )
}

function ManagePanel({ folder, report }) {
  const [confirming, setConfirming] = useState(false)
  return (
    <div className="shared-folders__manage">
      {confirming ? (
        <div className="shared-folders__confirm">
          <p className="settings__subtext">Stop sharing “{folder.name}”? The folder and its files stay on your device.</p>
          <div className="settings__confirm">
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => setConfirming(false)}>Cancel</button>
            <button type="button" className="settings__btn settings__btn--sm" onClick={async () => report(await removeFolder(folder.id))}>Remove</button>
          </div>
        </div>
      ) : (
        <div className="shared-folders__manage-actions">
          <button
            type="button"
            className="settings__btn settings__btn--outline settings__btn--sm"
            onClick={async () => report(await setFolderPaused(folder.id, !folder.paused))}
          >
            {folder.paused ? 'Resume' : 'Pause'}
          </button>
          <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => setConfirming(true)}>
            Remove
          </button>
        </div>
      )}
    </div>
  )
}
