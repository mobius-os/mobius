/* Words Settings uses for shared folders, kept in one place so every list says the same thing. */

/** Only a folder Chrome or Edge remembers can be edited; everything else is read-only. */
export function rightsLabel(folder) {
  return folder.mode === 'edit' && folder.access === 'remembered' ? 'Read & edit' : 'Read only'
}

export function folderCount(count) {
  return `${count} folder${count === 1 ? '' : 's'}`
}

const NEEDS_YOU = new Set(['needs_permission', 'needs_pick'])

/** Folders on this browser that agents cannot use until the owner taps once. */
export function needsYouCount(folders) {
  return folders.filter(folder => NEEDS_YOU.has(folder.state)).length
}

const OTHER_FOLDER_STATUS = {
  ready: { color: '--green', label: 'Ready', usable: true },
  paused: { color: '--muted', label: 'Paused' },
  offline: { color: '--muted', label: 'Offline' },
  needs_permission: { color: '--accent', label: 'Needs reconnect there' },
  needs_pick: { color: '--accent', label: 'Pick again there' },
}

/**
 * What agents can do with a folder on another browser, from that folder's own
 * state. A browser can be open while one of its folders is not usable (a closed
 * tab took a one-visit folder with it), so the browser being online says nothing
 * about this folder. Anything the server may add later reads as offline, not Ready.
 */
export function otherFolderStatus(folder) {
  if (!folder.online) return OTHER_FOLDER_STATUS.offline
  return OTHER_FOLDER_STATUS[folder.state] || OTHER_FOLDER_STATUS.offline
}
