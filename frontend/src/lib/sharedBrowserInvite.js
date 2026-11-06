/* Consume shared-browser admission hints without persisting them or retaining URL fragments. */
export function consumeSharedBrowserEntry(location, history) {
  const fragment = location.hash?.replace(/^#/, '') || ''
  const params = new URLSearchParams(fragment)
  if (fragment) history.replaceState(history.state, '', location.pathname + location.search)
  // Each hint names one admission: an invitation, an account sign-in, or an
  // account proof the recipient made to give their access up.
  const kinds = [['invite', 'invite'], ['account-finalize', 'account'], ['account-leave', 'leave']]
    .filter(([key]) => params.has(key))
  if (!kinds.length) return null
  if (kinds.length > 1) return { kind: 'invalid' }
  const [[key, kind]] = kinds
  const value = params.get(key)
  if (!value || params.getAll(key).length !== 1) return { kind: 'invalid' }
  return { kind, value }
}

export function watchSharedBrowserEntries(win, onEntry) {
  const admit = () => {
    const entry = consumeSharedBrowserEntry(win.location, win.history)
    if (entry) onEntry(entry)
  }
  win.addEventListener('hashchange', admit)
  return () => win.removeEventListener('hashchange', admit)
}
