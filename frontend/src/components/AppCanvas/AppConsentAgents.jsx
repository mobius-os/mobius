/* The "AI agents" block of the permission request: the agents' access scope
   (Tools, Read, Edit or Admin), what it grants in the platform's own words,
   the app's reason, and the tools it asks for (a tool that could carry app
   data outside Möbius says where, in amber). Admin (full access) leads with
   a fixed platform callout, because an app's reason cannot be trusted to state
   its own risk. The rows come from backend app_consent._agent_row. */

const ADMIN_LINE = 'Full access to your Möbius'

function AgentIcon() {
  return <svg className="app-consent__agents-icon" viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z" />
    <path d="M18 15l.9 2.1L21 18l-2.1.9L18 21l-.9-2.1L15 18l2.1-.9z" />
  </svg>
}

function WarningIcon() {
  return <svg className="app-consent__admin-icon" viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 3.5 2.5 20h19L12 3.5Z" />
    <path className="app-consent__admin-mark" d="M12 10v4.5M12 17.2v.3" />
  </svg>
}

export default function AppConsentAgents({ row }) {
  const admin = row.id === 'agent_start'
  const tools = row.tools || []
  return <section className={`app-consent__agents${admin ? ' app-consent__agents--admin' : ''}`} aria-label="AI agents">
    <div className="app-consent__agents-head">
      <AgentIcon />
      <strong>AI agents</strong>
      <span className={`app-consent__chip${admin ? ' app-consent__chip--critical' : ''}`}>{row.level}</span>
    </div>
    {admin
      ? <div className="app-consent__admin" role="note">
        <WarningIcon />
        <div>
          <strong>{ADMIN_LINE}</strong>
          <span>{row.grant}</span>
        </div>
      </div>
      : <p className="app-consent__agents-grant">{row.grant}</p>}
    {row.declared
      ? <div className="app-consent__agents-part">
        <p className="app-consent__agents-label">App’s reason</p>
        <p className="app-consent__agents-reason">{row.text}</p>
      </div>
      : <p className="app-consent__agents-reason">{row.text}</p>}
    {tools.length > 0 && <div className="app-consent__agents-part">
      <p className="app-consent__agents-label">Tools requested</p>
      <ul className="app-consent__agents-tools">
        {tools.map(tool => <li key={tool.id} className={tool.warning ? 'app-consent__tool--warning' : undefined}>
          <strong>{tool.title}</strong>
          <span>{tool.text}{tool.warning ? ` ${tool.warning}` : ''}</span>
        </li>)}
      </ul>
    </div>}
    {row.note && <p className="app-consent__agents-note">{row.note}</p>}
  </section>
}
