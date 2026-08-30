const STATUS_PRESENTATION = {
  added: { code: 'A', label: 'Added' },
  conflict: { code: '!', label: 'Conflict' },
  deleted: { code: 'D', label: 'Deleted' },
  modified: { code: 'M', label: 'Modified' },
  untracked: { code: '?', label: 'Untracked' },
}

export function gitStatusPresentation(status) {
  return STATUS_PRESENTATION[status] || { code: 'M', label: 'Changed' }
}

export function gitChangeCount(status) {
  if (!status?.available) return 0
  return Object.values(status.counts || {}).reduce(
    (sum, value) => sum + (Number.isFinite(value) ? value : 0),
    0,
  )
}

export function gitIdentityLabel(status) {
  if (!status?.available) return ''
  return status.branch || status.head || 'Git'
}

export function canInitializeProjectGit(status) {
  return status?.repository_scope !== 'project'
}

export function remoteSyncPresentation(status) {
  if (!status?.available) return { tone: 'quiet', title: 'Project history is not started', copy: 'Start a private history here before connecting GitHub.' }
  if (!status.connected) return { tone: 'quiet', title: 'No GitHub repository connected', copy: 'Connect an existing or new repository in owner/name form.' }
  if (status.diverged) return { tone: 'warn', title: 'Local and GitHub history diverged', copy: 'Review the branches outside this fast-forward flow before publishing.' }
  if (status.dirty) return { tone: 'warn', title: 'Commit local changes first', copy: 'Only reviewed commits are pushed; working files stay private.' }
  if (status.ahead && status.behind) return { tone: 'warn', title: 'Review both directions', copy: `${status.ahead} outgoing · ${status.behind} incoming` }
  if (status.ahead) return { tone: 'ready', title: `${status.ahead} ${status.ahead === 1 ? 'commit' : 'commits'} ready to push`, copy: 'Review the list below, then publish explicitly.' }
  if (status.behind) return { tone: 'info', title: `${status.behind} ${status.behind === 1 ? 'commit' : 'commits'} ready to pull`, copy: 'Your clean project can fast-forward safely.' }
  return { tone: 'ready', title: 'Up to date', copy: 'Local history and the tracked GitHub branch match.' }
}

export function remoteSyncActions(status) {
  const connected = !!status?.github_connected && !!status?.connected
  return {
    fetch: connected,
    pull: connected && !!status.behind && !status.dirty && !status.diverged,
    push: connected && !!status.ahead && !status.behind && !status.dirty && !status.diverged,
  }
}

export function gitAnnotationForEntry(changes, entry) {
  if (!entry?.path || !Array.isArray(changes)) return null
  if (entry.type !== 'directory') {
    const exact = changes.find(change => change.path === entry.path)
    if (!exact) return null
    return {
      kind: 'file',
      count: 1,
      status: exact.status,
      staged: !!exact.staged,
      ...gitStatusPresentation(exact.status),
    }
  }

  const prefix = `${entry.path.replace(/\/+$/, '')}/`
  const count = changes.reduce(
    (sum, change) => sum + (change.path?.startsWith(prefix) ? 1 : 0),
    0,
  )
  return count > 0
    ? { kind: 'directory', count, code: String(count), label: `${count} changed` }
    : null
}
