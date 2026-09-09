/* Project activity shows current file work and agent progress, separate from access. */
import { useRef } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { X, Sparkles } from '@openai/apps-sdk-ui/components/Icon'
import { api, jsonOrThrow } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import AgentCoordinationFeed from '../Agents/AgentCoordinationFeed.jsx'
import ProjectIdentityIcon from './ProjectIdentityIcon.jsx'
import './ProjectCollaborationPanel.css'
function agentState(run) {
  if (!run) return { label: 'Ready', active: false }
  if (run.status === 'running' || run.status === 'resume_pending') return { label: 'Working', active: true }
  if (run.status === 'parked' || run.status === 'parked_notified') return { label: 'Waiting', active: false }
  if (run.status === 'failed' || run.status === 'interrupted') return { label: 'Needs attention', active: false }
  if (run.status === 'stopped') return { label: 'Stopped', active: false }
  if (run.status === 'completed') return { label: 'Finished', active: false }
  return { label: 'Ready', active: false }
}

function initials(name) { return String(name || '?').trim().split(/\s+/).slice(0, 2).map(part => part[0]).join('').toUpperCase() }
export default function ProjectActivityPanel({ project, onClose }) {
  const cardRef = useRef(null)
  const closeRef = useRef(null)
  useDialogFocus({ containerRef: cardRef, initialFocusRef: closeRef, onClose })
  const agentsQuery = useQuery({
    queryKey: ['projects', 'agents', project.id],
    queryFn: async () => {
      const rows = await jsonOrThrow(await api.projects.agents(project.id), 'Agent activity failed:')
      return Array.isArray(rows) ? rows : []
    },
    refetchInterval: 10_000,
  })
  const coordinationQuery = useQuery({
    queryKey: ['agent-coordination', 'project', project.id],
    queryFn: async () => jsonOrThrow(
      await api.agentCoordination.project(project.id), 'Agent network failed:',
    ),
    refetchInterval: 5_000,
    staleTime: 1_500,
    retry: 0,
  })
  const claimsQuery = useQuery({
    queryKey: ['projects', 'work-claims', project.id],
    queryFn: async () => jsonOrThrow(
      await api.projects.workClaims(project.id), 'Active work failed:',
    ),
    refetchInterval: 5_000,
  })
  const agents = agentsQuery.data || []
  const claims = claimsQuery.data?.claims || []
  const humanClaims = claims.filter(claim => claim.actor_kind !== 'agent')
  const claimsByChat = new Map(
    claims.filter(claim => claim.chat_id).map(claim => [String(claim.chat_id), claim]),
  )
  const working = agents.filter(agent => (
    agentState(agent.run).active || claimsByChat.has(String(agent.id))
  )).length

  return createPortal(<div className="project-collab__overlay" onPointerDown={event => { if (event.target === event.currentTarget) onClose?.() }}>
    <aside ref={cardRef} className="project-collab" role="dialog" aria-modal="true" aria-labelledby="project-activity-title" tabIndex={-1}>
      <header className="project-collab__head"><ProjectIdentityIcon project={project} size={34} /><div><h2 id="project-activity-title">Activity</h2><span>{project.name}</span></div><button ref={closeRef} type="button" aria-label="Close activity panel" onClick={onClose}><X width={18} height={18} /></button></header>
      <div className="project-collab__body">
        {claimsQuery.isError && <button type="button" onClick={() => claimsQuery.refetch()}>Retry active work</button>}
        {!claimsQuery.isLoading && !claimsQuery.isError && humanClaims.length === 0 && <p className="project-collab__empty">No one is currently working in a file.</p>}
          {humanClaims.length > 0 && <section aria-labelledby="project-active-work-heading">
            <div className="project-collab__section-head"><h3 id="project-active-work-heading">Active work</h3><span>Files in use</span></div>
            <div className="project-collab__claims">
              {humanClaims.map(claim => <div key={claim.id} className="project-collab__claim">
                <span className="project-collab__avatar">{initials(claim.display_name)}</span>
                <span><strong>{claim.display_name}</strong><small>{claim.summary}</small></span>
                {claim.path && <i title={claim.path}>{claim.path.split('/').pop()}</i>}
              </div>)}
            </div>
          </section>}

          <section aria-labelledby="project-agents-heading">
            <div className="project-collab__section-head"><h3 id="project-agents-heading">Agents</h3><span>{working ? `${working} working` : 'Project chats'}</span></div>
            {agentsQuery.isLoading ? <p className="project-collab__empty">Loading agent activity…</p> : agentsQuery.isError ? <button type="button" className="project-collab__retry" onClick={() => agentsQuery.refetch()}>Retry agent activity</button> : agents.length === 0 ? <p className="project-collab__empty">Start a project chat to give an agent this workspace.</p> : <div className="project-collab__agents">
              {agents.map(agent => { const state = agentState(agent.run); const claim = claimsByChat.get(String(agent.id)); return <div key={agent.id} className="project-collab__agent"><span className={`project-collab__agent-icon${state.active || claim ? ' is-active' : ''}`}><Sparkles width={14} height={14} /></span><span><strong>{agent.title || 'Project agent'}</strong><small>{claim?.summary || agent.run?.summary || agent.run?.provider || 'Ready for project work'}</small></span><i>{state.label}</i></div> })}
            </div>}
            <AgentCoordinationFeed
              snapshot={coordinationQuery.data}
              loading={coordinationQuery.isLoading}
              error={coordinationQuery.isError}
              onRetry={() => coordinationQuery.refetch()}
            />
            <p className="project-collab__agent-note">Peer notes stay separate from your chats and remain visible here for review.</p>
          </section>

      </div>
    </aside>
  </div>, document.body)
}
