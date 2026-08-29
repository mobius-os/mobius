import { useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import ArrowLeft from 'lucide-react/dist/esm/icons/arrow-left.mjs'
import LogOut from 'lucide-react/dist/esm/icons/log-out.mjs'
import Users from 'lucide-react/dist/esm/icons/users.mjs'
import {
  api, BASE, clearEphemeralAuthSession, jsonOrThrow, setEphemeralAuthSession,
} from '../../api/client.js'
import ArtifactWorkspace from './ArtifactWorkspace.jsx'
import ProjectArtifacts from './ProjectArtifacts.jsx'
import ProjectFinder from './ProjectFinder.jsx'
import ProjectIdentityIcon from './ProjectIdentityIcon.jsx'
import './Projects.css'
import './ProjectShare.css'

const ROLE_LEVEL = { viewer: 0, editor: 1, maintainer: 2, owner: 3 }

function routeProjectId() {
  try {
    const prefix = `${BASE}/shared/project/`
    return window.location.pathname.startsWith(prefix)
      ? decodeURIComponent(window.location.pathname.slice(prefix.length).split('/')[0])
      : ''
  } catch {
    return ''
  }
}

function sessionKey(projectId) {
  return `mobius:project-session:${projectId}`
}

function initials(name) {
  return String(name || '?').trim().split(/\s+/).slice(0, 2).map(part => part[0]).join('').toUpperCase()
}

function releaseSplash() {
  const splash = document.getElementById('splash')
  if (!splash) return
  splash.style.pointerEvents = 'none'
  splash.style.opacity = '0'
  window.setTimeout(() => splash.remove(), 400)
}

export default function ProjectShare() {
  const queryClient = useQueryClient()
  const initialProjectId = routeProjectId()
  const [phase, setPhase] = useState('boot')
  const [projectId, setProjectId] = useState(initialProjectId)
  const [projectSeed, setProjectSeed] = useState(null)
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState('')
  const [joining, setJoining] = useState(false)
  const [artifactId, setArtifactId] = useState('')

  useEffect(() => {
    if (initialProjectId) {
      let token = ''
      try { token = sessionStorage.getItem(sessionKey(initialProjectId)) || '' } catch { /* ignore */ }
      if (token) {
        setEphemeralAuthSession(token, null)
        setPhase('workspace')
      } else {
        setPhase('expired')
      }
    } else {
      setPhase('invite')
    }
    releaseSplash()
  }, [initialProjectId])

  useEffect(() => {
    const expired = () => {
      if (projectId) {
        try { sessionStorage.removeItem(sessionKey(projectId)) } catch { /* ignore */ }
      }
      setPhase('expired')
    }
    window.addEventListener('mobius:ephemeral-auth-expired', expired)
    return () => window.removeEventListener('mobius:ephemeral-auth-expired', expired)
  }, [projectId])

  const projectQuery = useQuery({
    queryKey: ['shared-project', projectId],
    queryFn: async () => jsonOrThrow(await api.projects.detail(projectId), 'Project failed:'),
    enabled: phase === 'workspace' && !!projectId,
    initialData: projectSeed || undefined,
  })
  const collaborationQuery = useQuery({
    queryKey: ['shared-project', projectId, 'collaboration'],
    queryFn: async () => jsonOrThrow(
      await api.projects.collaboration(projectId), 'Project access failed:',
    ),
    enabled: phase === 'workspace' && !!projectId,
    refetchInterval: 10_000,
  })
  const refetchCollaboration = collaborationQuery.refetch
  const claimsQuery = useQuery({
    queryKey: ['shared-project', projectId, 'work-claims'],
    queryFn: async () => jsonOrThrow(
      await api.projects.workClaims(projectId), 'Active work failed:',
    ),
    enabled: phase === 'workspace' && !!projectId,
    refetchInterval: 5_000,
  })

  useEffect(() => {
    if (phase !== 'workspace' || !projectId) return undefined
    let active = true
    const beat = async () => {
      try {
        await api.projects.heartbeat(projectId)
        if (active) refetchCollaboration()
      } catch { /* the next real request owns auth/error feedback */ }
    }
    void beat()
    const timer = window.setInterval(beat, 25_000)
    return () => { active = false; window.clearInterval(timer) }
  }, [phase, projectId, refetchCollaboration])

  const project = projectQuery.data
  const collaboration = collaborationQuery.data || {}
  const role = collaboration.role || 'viewer'
  const canEdit = ROLE_LEVEL[role] >= ROLE_LEVEL.editor
  const canCommit = ROLE_LEVEL[role] >= ROLE_LEVEL.maintainer
  const members = collaboration.members || []
  const claims = claimsQuery.data?.claims || []
  const claimsByActor = new Map(claims.map(claim => [String(claim.actor_key), claim]))
  const online = members.filter(member => member.online).length
  const fileSource = useMemo(() => ({
    id: `shared:${projectId}:${role}`,
    readOnly: !canEdit,
    liveSync: true,
    filesKey: nextPath => ['shared-project', projectId, 'files', nextPath],
    gitStatusKey: () => ['shared-project', projectId, 'git', 'status'],
    gitDiffKey: nextPath => ['shared-project', projectId, 'git', 'diff', nextPath],
    files: (nextPath, options) => api.projects.files(projectId, nextPath, options),
    gitStatus: options => api.projects.gitStatus(projectId, options),
    gitDiff: (nextPath, options) => api.projects.gitDiff(projectId, nextPath, options),
    ...(canCommit ? {
      initGit: () => api.projects.initGit(projectId),
      commitGit: payload => api.projects.commitGit(projectId, payload),
    } : {}),
    readFile: (nextPath, options) => api.projects.readFile(projectId, nextPath, options),
    changes: (after, options) => api.projects.changes(projectId, after, options),
    claimWork: payload => api.projects.claimWork(projectId, payload),
    releaseWork: () => api.projects.releaseWork(projectId),
    ...(canEdit ? {
      writeFile: (nextPath, content, expectedRevision) => (
        api.projects.writeFile(projectId, nextPath, content, expectedRevision)
      ),
      writeBytes: (nextPath, bytes, expectedRevision) => (
        api.projects.writeBytes(projectId, nextPath, bytes, expectedRevision)
      ),
      createFolder: nextPath => api.projects.createFolder(projectId, nextPath),
      deleteFile: nextPath => api.projects.deleteFile(projectId, nextPath),
      move: payload => api.projects.move(projectId, payload),
    } : {}),
    invalidate: client => Promise.all([
      client.invalidateQueries({ queryKey: ['shared-project', projectId, 'files'] }),
      client.invalidateQueries({ queryKey: ['shared-project', projectId, 'git'] }),
    ]),
  }), [projectId, role, canEdit, canCommit])

  async function join(event) {
    event.preventDefault()
    const name = displayName.trim()
    const secret = window.location.hash.replace(/^#/, '')
    if (!name || !secret || joining) return
    setJoining(true); setError('')
    try {
      const payload = await jsonOrThrow(await api.projects.redeemInvite({
        invite: secret, display_name: name,
      }), 'Invitation failed:')
      setEphemeralAuthSession(payload.access_token, null)
      try { sessionStorage.setItem(sessionKey(payload.project.id), payload.access_token) } catch { /* tab remains usable */ }
      setProjectId(payload.project.id)
      setProjectSeed(payload.project)
      window.history.replaceState(null, '', `${BASE}/shared/project/${encodeURIComponent(payload.project.id)}`)
      setPhase('workspace')
    } catch (cause) {
      setError(cause?.message || 'This invitation could not be accepted.')
    } finally { setJoining(false) }
  }

  function leave() {
    try { sessionStorage.removeItem(sessionKey(projectId)) } catch { /* ignore */ }
    clearEphemeralAuthSession()
    setPhase('expired')
  }

  if (phase === 'boot') return <div className="project-share project-share--center" aria-busy="true" />
  if (phase === 'invite') return (
    <main className="project-share project-share--center">
      <section className="project-share__join" aria-labelledby="project-share-join-title">
        <span className="project-share__mark"><Users size={22} /></span>
        <p>Private project invitation</p>
        <h1 id="project-share-join-title">Work together, right here</h1>
        <span>Your access is limited to the project in this invitation. The owner can change your role or remove access at any time.</span>
        <form onSubmit={join}>
          <label htmlFor="project-share-name">Your name</label>
          <input id="project-share-name" autoFocus autoComplete="name" maxLength={128} value={displayName} placeholder="How collaborators will see you" onChange={event => setDisplayName(event.target.value)} />
          <button type="submit" disabled={joining || !displayName.trim() || !window.location.hash}>{joining ? 'Joining…' : 'Join project'}</button>
        </form>
        {error && <div className="project-share__error" role="alert">{error}</div>}
      </section>
    </main>
  )
  if (phase === 'expired') return (
    <main className="project-share project-share--center">
      <section className="project-share__join" role="alert">
        <span className="project-share__mark"><Users size={22} /></span>
        <p>Project access</p><h1>This session has ended</h1>
        <span>Ask the project owner for a new invitation if you still need access.</span>
      </section>
    </main>
  )

  async function buildFileAsArtifact(path, builder) {
    if (!canEdit) return
    const base = (path.split('/').pop() || path).replace(/\.[^.]+$/, '') || 'output'
    const id = path.replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9_-]+/g, '-')
      .replace(/^-+|-+$/g, '').toLowerCase().slice(0, 64) || 'artifact'
    try {
      const create = await api.projects.createArtifact(projectId, { id, name: base, builder, source: path })
      if (!create.ok && create.status !== 409) await jsonOrThrow(create, 'Build failed:')
      await jsonOrThrow(await api.projects.buildArtifact(projectId, id), 'Build failed:')
      await queryClient.invalidateQueries({ queryKey: ['projects', 'artifacts', projectId] })
      setArtifactId(id)
    } catch (cause) {
      setError(cause?.message || 'Could not build that file.')
    }
  }

  async function rebuildRegisteredArtifacts() {
    if (!canEdit) return
    const artifacts = await jsonOrThrow(
      await api.projects.artifacts(projectId),
      'Artifact refresh failed:',
    )
    const ready = (Array.isArray(artifacts) ? artifacts : []).filter(
      artifact => artifact?.status !== 'building' && !artifact?.source_missing,
    )
    const outcomes = await Promise.allSettled(ready.map(async artifact => (
      jsonOrThrow(await api.projects.buildArtifact(projectId, artifact.id), 'Build failed:')
    )))
    await queryClient.invalidateQueries({ queryKey: ['projects', 'artifacts', projectId] })
    const failed = outcomes.find(outcome => outcome.status === 'rejected')
    if (failed) throw failed.reason
  }

  if (projectQuery.isLoading || collaborationQuery.isLoading) return <div className="project-share project-share--center"><p role="status">Opening project…</p></div>
  if (!project || projectQuery.isError || collaborationQuery.isError) return <div className="project-share project-share--center"><section className="project-share__join" role="alert"><h1>Couldn’t open this project</h1><span>{projectQuery.error?.message || collaborationQuery.error?.message || 'Try the invitation again.'}</span></section></div>

  return (
    <main className="project-share">
      <header className="project-share__header">
        <ProjectIdentityIcon project={project} size={36} />
        <div><strong>{project.name}</strong><small>{role} · {online} online</small></div>
        <button type="button" onClick={leave}><LogOut size={16} /><span>Leave</span></button>
      </header>
      <div className="project-share__workspace">
        {artifactId ? <ArtifactWorkspace projectId={projectId} artifactId={artifactId} projectName={project.name} readOnly={!canEdit} onOpenProject={() => setArtifactId('')} /> : <ProjectFinder
          projectId={projectId}
          projectName={project.name}
          artifactTypes={project.template?.artifact_types}
          fileSource={fileSource}
          onBuildFile={canEdit ? buildFileAsArtifact : undefined}
          onSourceSaved={canEdit ? rebuildRegisteredArtifacts : undefined}
          overview={<div className="project-share__overview">
            <section><div className="project-share__section-head"><h2>Artifacts</h2></div><ProjectArtifacts projectId={projectId} onOpen={setArtifactId} /></section>
            <section><div className="project-share__section-head"><h2>People</h2><span>{online} online</span></div><div className="project-share__people">{members.map(member => { const claim = claimsByActor.get(member.id === 'owner' ? 'owner' : `member:${member.id}`); return <div key={member.id}><span>{initials(member.display_name)}</span><strong>{member.you ? `${member.display_name} · You` : member.display_name}</strong><small>{claim?.summary || `${member.role}${member.online ? ' · online' : ''}`}</small></div> })}{claims.filter(claim => claim.actor_kind === 'agent').map(claim => <div key={claim.id}><span>AI</span><strong>{claim.display_name}</strong><small>{claim.summary}</small></div>)}</div></section>
          </div>}
        />}
      </div>
      {error && <div className="project-share__toast" role="alert"><button type="button" aria-label="Dismiss" onClick={() => setError('')}><ArrowLeft size={14} /></button>{error}</div>}
    </main>
  )
}
