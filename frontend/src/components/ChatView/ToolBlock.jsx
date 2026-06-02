import { useState } from 'react'

export default function ToolBlock({ t }) {
  const [open, setOpen] = useState(false)
  const label = t.tool + (t.input ? `: ${t.input}` : '')
  const hasDetail = !!(t.input || t.output)

  return (
    <div className={`chat__tool chat__tool--${t.status || 'done'}`}>
      <div className="chat__tool-header" onClick={() => hasDetail && setOpen(!open)}>
        {t.status === 'running' && <span className="chat__tool-spin" />}
        {/* Skill observability: when the Skill tool loaded a named
            skill, show its name as a chip so the user can see which
            skill the agent reached for this turn. */}
        {t.skill && <span className="chat__tool-chip">skill: {t.skill}</span>}
        <span className="chat__tool-name">
          {t.status === 'running' ? `Running ${t.tool}...` : label}
        </span>
        {hasDetail && <span className="chat__tool-toggle">{open ? '▾' : '▸'}</span>}
      </div>
      {open && (t.input || t.output) && (
        <pre className="chat__tool-detail">
          {t.input && <>{t.input}{'\n'}</>}
          {t.output && <span className="chat__tool-output">{t.output}</span>}
        </pre>
      )}
    </div>
  )
}
