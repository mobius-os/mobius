/* Keep GitHub collaboration links bound to a parsed repository identity. */
export function githubCollaborationLink(status) {
  if (!status?.connected || typeof status.repository !== 'string') return null
  const parts = status.repository.split('/')
  if (parts.length !== 2 || !parts.every(part => /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(part))) return null
  return `https://github.com/${parts.join('/')}/fork`
}
