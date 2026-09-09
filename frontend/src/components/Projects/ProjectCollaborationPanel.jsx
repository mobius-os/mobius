import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Copy from 'lucide-react/dist/esm/icons/copy.mjs'
import Crown from 'lucide-react/dist/esm/icons/crown.mjs'
import Eye from 'lucide-react/dist/esm/icons/eye.mjs'
import Pencil from 'lucide-react/dist/esm/icons/pencil.mjs'
import ShieldCheck from 'lucide-react/dist/esm/icons/shield-check.mjs'
import Trash2 from 'lucide-react/dist/esm/icons/trash-2.mjs'
import UserPlus from 'lucide-react/dist/esm/icons/user-plus.mjs'
import X from 'lucide-react/dist/esm/icons/x.mjs'
import { api, jsonOrThrow } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { linkedProjectAppId } from '../../lib/appSourceProject.js'
import ProjectIdentityIcon from './ProjectIdentityIcon.jsx'
import ProjectCopyPanel from './ProjectCopyPanel.jsx'
import ProjectGithubSharing from './ProjectGithubSharing.jsx'
import { ArrowLeft } from '@openai/apps-sdk-ui/components/Icon'
import './ProjectCollaborationPanel.css'

const ROLES = [
  { name: 'Owner', Icon: Crown, copy: 'People, commits, agents, and publishing' },
  { name: 'Maintainer', Icon: ShieldCheck, copy: 'Files, previews, and local commits' },
  { name: 'Editor', Icon: Pencil, copy: 'Files, previews, and builds' },
  { name: 'Viewer', Icon: Eye, copy: 'Files and built outputs' },
]
const SHARE_ROLES = ROLES.filter(role => role.name !== 'Owner')

function initials(name) {
  return String(name || '?').trim().split(/\s+/).slice(0, 2).map(part => part[0]).join('').toUpperCase()
}

function ProjectLiveCollaboration({ project }) {
  const queryClient = useQueryClient()
  const [inviteOpen, setInviteOpen] = useState(false)
  const [inviteeName, setInviteeName] = useState('')
  const [inviteRole, setInviteRole] = useState('editor')
  const [createdInvite, setCreatedInvite] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  const collaborationKey = useMemo(
    () => ['projects', 'collaboration', project.id],
    [project.id],
  )
  const collaborationQuery = useQuery({
    queryKey: collaborationKey,
    queryFn: async () => jsonOrThrow(
      await api.projects.collaboration(project.id), 'Project access failed:',
    ),
    refetchInterval: 10_000,
  })
  useEffect(() => {
    let active = true
    const beat = async () => {
      try {
        await api.projects.heartbeat(project.id)
        if (active) queryClient.invalidateQueries({ queryKey: collaborationKey })
      } catch { /* presence is helpful, never a gate on sharing */ }
    }
    void beat()
    const timer = window.setInterval(beat, 25_000)
    return () => { active = false; window.clearInterval(timer) }
  }, [project.id, queryClient, collaborationKey])

  const collaboration = collaborationQuery.data || {}
  const members = collaboration.members || []
  const invites = collaboration.invites || []
  async function createInvite(event) {
    event.preventDefault()
    if (busy) return
    setBusy('invite'); setError(''); setCopied(false)
    try {
      const invite = await jsonOrThrow(await api.projects.createInvite(project.id, {
        invitee_name: inviteeName.trim() || null,
        role: inviteRole,
      }), 'Invitation failed:')
      setCreatedInvite(invite)
      setInviteeName('')
      await collaborationQuery.refetch()
    } catch (cause) {
      setError(cause?.message || 'Could not create that invitation.')
    } finally { setBusy('') }
  }

  async function copyInvite() {
    if (!createdInvite?.join_url) return
    try {
      await navigator.clipboard.writeText(createdInvite.join_url)
      setCopied(true)
    } catch {
      setError('Copy failed. Select the invitation link and copy it manually.')
    }
  }

  async function changeRole(memberId, role) {
    setBusy(memberId); setError('')
    try {
      await jsonOrThrow(await api.projects.updateMember(project.id, memberId, { role }), 'Role update failed:')
      await collaborationQuery.refetch()
    } catch (cause) {
      setError(cause?.message || 'Could not update that role.')
    } finally { setBusy('') }
  }

  async function removeMember(memberId, name) {
    if (!window.confirm(`Remove ${name} from this project?`)) return
    setBusy(memberId); setError('')
    try {
      const response = await api.projects.revokeMember(project.id, memberId)
      if (!response.ok) await jsonOrThrow(response, 'Removal failed:')
      await collaborationQuery.refetch()
    } catch (cause) {
      setError(cause?.message || 'Could not remove that person.')
    } finally { setBusy('') }
  }

  async function revokeInvite(inviteId) {
    setBusy(inviteId); setError('')
    try {
      const response = await api.projects.revokeInvite(project.id, inviteId)
      if (!response.ok) await jsonOrThrow(response, 'Invitation removal failed:')
      if (createdInvite?.id === inviteId) setCreatedInvite(null)
      await collaborationQuery.refetch()
    } catch (cause) {
      setError(cause?.message || 'Could not revoke that invitation.')
    } finally { setBusy('') }
  }

  return <>
    <p className="project-sharing__description">Edit the same project together. People join with your invitation link—no GitHub account or Möbius of their own needed.</p>
          {linkedProjectAppId(project) && <p className="project-collab__empty">Share the app’s source files here. Only the owner can update the installed app; access to its data is separate.</p>}
          <section aria-labelledby="project-people-heading">
            <div className="project-collab__section-head"><h3 id="project-people-heading">People</h3><span>Private · {members.length} {members.length === 1 ? 'person' : 'people'}</span></div>
            {collaborationQuery.isError ? <button type="button" onClick={() => collaborationQuery.refetch()}>Retry people</button> : collaborationQuery.isLoading ? <p className="project-collab__empty">Loading people…</p> : members.map(member => (
              <div className="project-collab__person" key={member.id}>
                <span className="project-collab__avatar">{initials(member.display_name)}</span>
                <span><strong>{member.you ? `${member.display_name} · You` : member.display_name}</strong><small>{member.role} · {ROLES.find(role => role.name.toLowerCase() === member.role)?.copy}</small></span>
                {member.id === 'owner' ? <span className={member.online ? 'project-collab__online' : 'project-collab__offline'}>{member.online ? 'Online' : 'Away'}</span> : <div className="project-collab__member-actions">
                  <select className="project-collab__role-picker" aria-label={`Role for ${member.display_name}`} value={member.role} disabled={busy === member.id} onChange={event => void changeRole(member.id, event.target.value)}>
                    {SHARE_ROLES.map(({ name }) => <option key={name} value={name.toLowerCase()}>{name}</option>)}
                  </select>
                  <button type="button" className="project-collab__remove" aria-label={`Remove ${member.display_name}`} disabled={busy === member.id} onClick={() => void removeMember(member.id, member.display_name)}><Trash2 size={14} /></button>
                </div>}
              </div>
            ))}

            {!inviteOpen ? <button type="button" className="project-collab__invite-toggle" onClick={() => { setInviteOpen(true); setCreatedInvite(null); setError('') }}><UserPlus size={15} /> Invite someone</button> : <form className="project-collab__invite" onSubmit={createInvite}>
              <label>Who is this for?<input autoFocus value={inviteeName} maxLength={128} placeholder="Name (optional)" onChange={event => setInviteeName(event.target.value)} /></label>
              <fieldset className="project-collab__invite-roles"><legend>Access</legend><div role="radiogroup" aria-label="Invitation access">
                {SHARE_ROLES.map(({ name, Icon, copy }) => { const value = name.toLowerCase(); return <button key={name} type="button" role="radio" aria-checked={inviteRole === value} onClick={() => setInviteRole(value)}><Icon size={14} /><span><strong>{name}</strong><small>{copy}</small></span></button> })}
              </div></fieldset>
              <div><button type="submit" disabled={busy === 'invite'}>{busy === 'invite' ? 'Creating…' : 'Create invitation'}</button><button type="button" onClick={() => { setInviteOpen(false); setCreatedInvite(null) }}>Cancel</button></div>
            </form>}

            {createdInvite && <div className="project-collab__invite-result" role="status">
              <strong>Invitation ready</strong><p>Send this private link to {createdInvite.invitee_name || 'your collaborator'}. It works once and expires in 7 days.</p>
              <div><input readOnly value={createdInvite.join_url} aria-label="Invitation link" onFocus={event => event.currentTarget.select()} /><button type="button" onClick={copyInvite}><Copy size={14} /> {copied ? 'Copied' : 'Copy'}</button></div>
            </div>}

            {invites.length > 0 && <div className="project-collab__pending"><small>Pending invitations</small>{invites.map(invite => <div key={invite.id}><span><strong>{invite.invitee_name || 'Invitation link'}</strong><small>{invite.role}</small></span><button type="button" aria-label={`Revoke invitation for ${invite.invitee_name || 'collaborator'}`} disabled={busy === invite.id} onClick={() => void revokeInvite(invite.id)}><X size={14} /></button></div>)}</div>}
          </section>

          {error && <p className="projects-error" role="alert">{error}</p>}
  </>
}

export default function ProjectCollaborationPanel({ project, onClose, onOpenGithub }) {
  const [mode, setMode] = useState(null)
  const cardRef = useRef(null)
  const closeRef = useRef(null)
  useDialogFocus({ containerRef: cardRef, initialFocusRef: closeRef, onClose })
  const title = mode === 'copy' ? 'Share a copy' : mode === 'live' ? 'Work together live' : mode === 'github' ? 'Work together with GitHub' : 'Share project'
  return createPortal(
    <div className="project-collab__overlay" onPointerDown={event => { if (event.target === event.currentTarget) onClose?.() }}>
      <aside ref={cardRef} className="project-collab" role="dialog" aria-modal="true" aria-labelledby="project-collab-title" tabIndex={-1}>
        <header className="project-collab__head">
          <ProjectIdentityIcon project={project} size={34} />
          <div><h2 id="project-collab-title">{title}</h2><span>{project.name}</span></div>
          <button ref={closeRef} type="button" aria-label="Close sharing panel" onClick={onClose}><X size={18} /></button>
        </header>
        <div className="project-collab__body">
          {mode && <button type="button" className="project-sharing__back" onClick={() => setMode(null)}><ArrowLeft width={16} height={16} /> Sharing options</button>}
          {!mode && <>
            <section className="project-sharing__group" aria-labelledby="project-share-copy-heading">
              <h3 id="project-share-copy-heading">Give someone a copy</h3>
              <button type="button" className="project-sharing__choice" onClick={() => setMode('copy')}>
                <strong>Share a copy</strong>
                <span>A separate, editable copy.</span>
                <small>No GitHub needed</small>
              </button>
            </section>
            <section className="project-sharing__group" aria-labelledby="project-share-together-heading">
              <h3 id="project-share-together-heading">Work together</h3>
              <button type="button" className="project-sharing__choice" onClick={() => setMode('live')}>
                <strong>Work together live</strong>
                <span>Edit the same project.</span>
                <small>No GitHub needed</small>
              </button>
              <button type="button" className="project-sharing__choice" onClick={() => setMode('github')}>
                <strong>Work together with GitHub</strong>
                <span>Separate versions, reviewed changes.</span>
                <small>GitHub accounts needed</small>
              </button>
            </section>
          </>}
          {mode === 'copy' && <ProjectCopyPanel key={project.id} project={project} />}
          {mode === 'live' && <ProjectLiveCollaboration key={project.id} project={project} />}
          {mode === 'github' && <ProjectGithubSharing key={project.id} project={project} onOpenGithub={onOpenGithub} />}
        </div>
      </aside>
    </div>, document.body,
  )
}
