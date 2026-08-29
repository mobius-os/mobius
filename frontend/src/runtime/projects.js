// window.mobius.projects — a light "named project workspaces" registry.
//
// A general primitive: an app that wants named projects (a switcher, per-project
// files/chat) can list/create/rename/remove them here instead of hand-rolling a
// registry. It owns ONLY the list ({id, name, created_at, updated_at}); the
// app keeps each project's files in its own storage (conventionally under a
// `projects/<id>/` prefix) and does its own building/publishing. Every call
// rides the app runtime token, so the backend scopes the list to the app.

import { fetchWithAppToken } from './network.js'

async function jsonOrThrow(res, action) {
  if (!res.ok) {
    let detail = ''
    try { detail = (await res.json())?.detail || '' } catch (e) {}
    throw new Error(detail || `mobius.projects: ${action} failed (${res.status})`)
  }
  if (res.status === 204) return null
  return res.json()
}

export function makeProjects({ getToken }) {
  const call = (path, init) =>
    fetchWithAppToken(getToken, `/api/projects${path}`, init)
  const jsonBody = (method, body) => ({
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  return {
    // List this app's projects, most-recently-updated first.
    async list() {
      return jsonOrThrow(await call(''), 'list')
    },
    // Create a project. Pass an explicit `id` to keep a fixed one (e.g. a
    // "default"); it is idempotent for a supplied id.
    async create(name, id) {
      return jsonOrThrow(await call('', jsonBody('POST', { name, id })), 'create')
    },
    async rename(id, name) {
      return jsonOrThrow(
        await call(`/${encodeURIComponent(id)}`, jsonBody('PATCH', { name })),
        'rename',
      )
    },
    // Remove the registry entry only — the app clears the project's files.
    async remove(id) {
      return jsonOrThrow(
        await call(`/${encodeURIComponent(id)}`, { method: 'DELETE' }),
        'remove',
      )
    },
  }
}
