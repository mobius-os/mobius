/* The "AI agents" block of the permission request: the app's one agent role,
   the app's own reason, and the agent tools it declared. Owner (full access)
   adds a fixed platform line, because an app's reason cannot be trusted to
   state its own risk. The rows come from backend app_consent._agent_row. */

const OWNER_LINE = 'Full access to your Möbius.'
const WEB_WARNING = 'App data may reach the web.'

function AgentIcon() {
  return <svg className="app-consent__agents-icon" viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z" />
    <path d="M18 15l.9 2.1L21 18l-2.1.9L18 21l-.9-2.1L15 18l2.1-.9z" />
  </svg>
}

export default function AppConsentAgents({ row }) {
  const owner = row.id === 'agent_start'
  const tools = row.tools || []
  return <section className="app-consent__agents" aria-label="AI agents">
    <div className="app-consent__agents-head">
      <AgentIcon />
      <strong>AI agents</strong>
      <span className={`app-consent__chip${owner ? ' app-consent__chip--critical' : ''}`}>{row.level}</span>
      {row.critical && <span className="app-consent__dot" role="img" aria-label="Critical: can change your Möbius" title="Critical: can change your Möbius" />}
    </div>
    <p className="app-consent__agents-why">{row.text}</p>
    {owner && <p className="app-consent__agents-owner">{OWNER_LINE}</p>}
    {tools.length > 0 && <>
      <p className="app-consent__agents-label">Agent tools</p>
      <ul className="app-consent__agents-tools">
        {tools.map(tool => <li key={tool.id} className={tool.warning ? 'app-consent__tool--warning' : undefined}>
          <strong>{tool.title}</strong>
          <span>{tool.text}{tool.warning ? ` ${WEB_WARNING}` : ''}</span>
        </li>)}
      </ul>
    </>}
  </section>
}
