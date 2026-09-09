/* Source-copy transport and fragment-only navigation keep sharing separate from live access. */
import { apiFetch, BASE, jsonOrThrow } from '../api/client.js'

export async function projectCopyRequest(path, { method = 'GET', body, signal } = {}) {
  const response = await apiFetch(`/project-copies${path}`, {
    method, signal,
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  })
  if (response.ok && response.status === 204) return null
  return jsonOrThrow(response, 'Could not load project copy')
}

export async function readPublicProjectCopy(token, signal) {
  return jsonOrThrow(await fetch(`${BASE}/api/project-copies/metadata`, {
    method: 'POST', credentials: 'omit', signal,
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }),
  }), 'This copy link is unavailable')
}

export function projectCopyDestination(address, copyUrl) {
  let destination
  try { destination = new URL(address.trim()) } catch { throw new Error('Enter the full address of your Möbius, starting with https://.') }
  if (!['http:', 'https:'].includes(destination.protocol) || destination.username || destination.password) {
    throw new Error('Use an http:// or https:// address without a username or password.')
  }
  destination.search = ''
  destination.hash = ''
  // Preserve deployment prefixes, accepting either the home address or /shell/.
  const base = destination.pathname.replace(/\/+$/, '').replace(/\/shell$/, '')
  destination.pathname = `${base}/shell/`
  destination.hash = new URLSearchParams({ 'project-copy': copyUrl }).toString()
  return destination.href
}

export function readProjectCopyRequest(href) {
  try { return new URLSearchParams(new URL(href).hash.slice(1)).get('project-copy') || '' } catch { return '' }
}

export function clearProjectCopyRequest(href) {
  const url = new URL(href)
  const fragment = new URLSearchParams(url.hash.slice(1))
  fragment.delete('project-copy')
  url.hash = fragment.toString()
  return url.href
}

export function copyByteLabel(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function copyDate(value) {
  return new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`)
}

const PENDING_COPY_KEY = 'mobius:pending-project-copy'

/** Keep one incoming copy in tab-local memory across the external identity redirect, never a query. */
export function rememberProjectCopyRequest(href, session) {
  const request = readProjectCopyRequest(href)
  if (request) {
    try { session.setItem(PENDING_COPY_KEY, request) } catch { /* the original fragment still owns local login */ }
  }
  return request
}

/** Consume once on entering the owner's shell; cancel and success must not reopen a stale import. */
export function consumeProjectCopyRequest(href, session) {
  const incoming = readProjectCopyRequest(href)
  let remembered = ''
  try { remembered = session.getItem(PENDING_COPY_KEY) || ''; session.removeItem(PENDING_COPY_KEY) } catch { /* unavailable tab storage leaves the incoming fragment usable */ }
  return incoming || remembered
}

/** Selections belong to one reviewed snapshot; loading and replacement snapshots use their own defaults. */
export function selectedCopyPaths(selection, preview) {
  if (selection && preview && selection.digest === preview.digest) return selection.paths
  return (preview?.files || []).filter(file => file.selected).map(file => file.path)
}
