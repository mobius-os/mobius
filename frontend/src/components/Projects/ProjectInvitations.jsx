import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import ExternalLink from 'lucide-react/dist/esm/icons/external-link.mjs'
import X from 'lucide-react/dist/esm/icons/x.mjs'
import { api, jsonOrThrow } from '../../api/client.js'
import ProjectTypeIcon from './ProjectTypeIcon.jsx'
import './ProjectInvitations.css'

const RECEIVED_INVITES_KEY = ['projects', 'received-invites']

function roleLabel(role) {
  return role ? role[0].toUpperCase() + role.slice(1) : 'Editor'
}

// The shared workspace lives on the inviter's Möbius. Open its tab during the
// click itself so a popup blocker allows it, then point it at the URL once the
// server has answered (or close it if that failed).
async function openInNewTab(loadUrl) {
  const tab = window.open('about:blank', '_blank')
  try {
    const url = await loadUrl()
    if (tab) {
      tab.opener = null
      tab.location.replace(url)
    } else {
      window.location.assign(url)
    }
  } catch (cause) {
    tab?.close()
    throw cause
  }
}

// Invitations other people sent to this Möbius by @handle, and the projects
// they have shared that were accepted here.
export default function ProjectInvitations() {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const query = useQuery({
    queryKey: RECEIVED_INVITES_KEY,
    queryFn: async () => jsonOrThrow(await api.projects.receivedInvites(), 'Invitations failed:'),
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  })
  const rows = Array.isArray(query.data) ? query.data : []
  const pending = rows.filter(row => row.status === 'pending')
  const shared = rows.filter(row => row.status === 'accepted')
  // Expired invitations stay listed until removed, but nothing is waiting on them.
  const waiting = pending.filter(row => !row.expired).length
  if (!pending.length && !shared.length) return null

  async function act(row, kind, run) {
    if (busy) return
    setBusy(`${kind}:${row.id}`); setError('')
    try {
      await run()
    } catch (cause) {
      setError(cause?.message || 'That didn’t work. Try again.')
    } finally {
      setBusy('')
      void query.refetch()
    }
  }

  const accept = row => act(row, 'accept', () => openInNewTab(async () => (
    await jsonOrThrow(await api.projects.acceptReceivedInvite(row.id), 'Accept failed:')
  ).open_url))
  const open = row => act(row, 'open', () => openInNewTab(async () => (
    await jsonOrThrow(await api.projects.openReceivedProject(row.id), 'Open failed:')
  ).url))
  const decline = row => act(row, 'decline', async () => {
    await jsonOrThrow(await api.projects.declineReceivedInvite(row.id), 'Decline failed:')
  })
  const remove = row => act(row, 'remove', async () => {
    const response = await api.projects.removeReceivedInvite(row.id)
    if (!response.ok) await jsonOrThrow(response, 'Remove failed:')
  })

  return (
    <div className="project-invitations projects-collection">
      {pending.length > 0 && <section aria-labelledby="project-invitations-heading">
        <h2 id="project-invitations-heading">Invitations {waiting > 0 && <span>{waiting}</span>}</h2>
        <div className="project-invitations__list">
          {pending.map(row => (
            <div className="project-invitations__invite" key={row.id}>
              <span className="project-invitations__icon"><ProjectTypeIcon value={row.project_type} size={20} aria-hidden="true" /></span>
              <span className="projects-collection__copy">
                <strong>{row.project_name}</strong>
                <small>{row.expired ? `@${row.from_handle}’s invitation expired` : `@${row.from_handle} invited you · ${roleLabel(row.role)}`}</small>
              </span>
              <span className="project-invitations__actions">
                {row.expired
                  ? <button type="button" disabled={!!busy} onClick={() => void remove(row)}>Remove</button>
                  : <>
                    <button type="button" className="project-invitations__accept" disabled={!!busy} onClick={() => void accept(row)}>{busy === `accept:${row.id}` ? 'Joining…' : 'Accept'}</button>
                    <button type="button" disabled={!!busy} onClick={() => void decline(row)}>Decline</button>
                  </>}
              </span>
            </div>
          ))}
        </div>
      </section>}

      {shared.length > 0 && <section aria-labelledby="project-shared-heading">
        <h2 id="project-shared-heading">Shared with you</h2>
        <div className="projects-collection--list">
          {shared.map(row => (
            <div className="projects-collection__row" key={row.id}>
              <button type="button" className="projects-collection__main" disabled={!!busy || row.access_expired} onClick={() => void open(row)}>
                <span className="project-invitations__icon"><ProjectTypeIcon value={row.project_type} size={20} aria-hidden="true" /></span>
                <span className="projects-collection__copy">
                  <strong>{row.project_name}</strong>
                  <small>{row.access_expired ? `Access expired · ask @${row.from_handle} to invite you again` : `@${row.from_handle}’s project · ${roleLabel(row.role)}`}</small>
                </span>
                {!row.access_expired && <ExternalLink className="project-invitations__external" size={15} aria-label="Opens in a new tab" />}
              </button>
              <button
                type="button"
                className="project-invitations__remove"
                aria-label={`Remove ${row.project_name} from your list`}
                title="Remove from your list"
                disabled={!!busy}
                onClick={() => {
                  if (window.confirm(`Remove “${row.project_name}” from your Projects? @${row.from_handle} still controls your access to it.`)) void remove(row)
                }}
              ><X size={15} /></button>
            </div>
          ))}
        </div>
      </section>}
      {error && <p className="projects-error" role="alert">{error}</p>}
    </div>
  )
}
