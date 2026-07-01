import { useEffect, useState } from 'react'
import { apiFetch } from '../../api/client.js'

export default function ToolBlock({ t, chatId, msgTs, blockIdx }) {
  const [open, setOpen] = useState(() => !!t.defaultOpen)
  // The full output of a large tool block is fetched lazily on first expand —
  // a chat load ships only the top-line summary (the tool + its input) and an
  // output_truncated marker, no output preview (see chats.py
  // _truncate_large_tool_outputs), so a Read of a huge file or a long bash run
  // doesn't bloat the payload for blocks the user never opens. Cached here so
  // re-collapsing doesn't refetch.
  const [fullOutput, setFullOutput] = useState(null)
  const [loadingFull, setLoadingFull] = useState(false)
  const toolName = t.tool || 'Tool'
  const label = toolName + (t.input ? `: ${t.input}` : '')
  const hasDetail = !!(t.input || t.output || t.output_truncated)

  useEffect(() => {
    if (t.defaultOpen) setOpen(true)
  }, [t.defaultOpen])

  useEffect(() => {
    if (!open || !t.output_truncated || fullOutput !== null || loadingFull) return
    if (!chatId || msgTs == null || blockIdx == null) return
    let cancelled = false
    setLoadingFull(true)
    apiFetch(`/chats/${chatId}/tool-output?ts=${msgTs}&i=${blockIdx}`)
      .then(res => (res.ok ? res.text() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then(text => { if (!cancelled) setFullOutput(text) })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoadingFull(false) })
    return () => { cancelled = true }
  }, [open, t.output_truncated, fullOutput, loadingFull, chatId, msgTs, blockIdx])

  // Show the fetched full output once it lands; until then the inline preview.
  const shownOutput = t.output_truncated && fullOutput !== null ? fullOutput : t.output

  return (
    <div className={`chat__tool chat__tool--${t.status || 'done'}`}>
      <div className="chat__tool-header" onClick={() => hasDetail && setOpen(!open)}>
        {t.status === 'running' && <span className="chat__tool-spin" />}
        {/* Skill observability: when the Skill tool loaded a named
            skill, show its name as a chip so the user can see which
            skill the agent reached for this turn. */}
        {t.skill && <span className="chat__tool-chip">skill: {t.skill}</span>}
        <span className="chat__tool-name">
          {t.status === 'running' ? `Running ${toolName}...` : label}
        </span>
        {hasDetail && <span className="chat__tool-toggle">{open ? '▾' : '▸'}</span>}
      </div>
      {open && (t.input || shownOutput) && (
        <pre className="chat__tool-detail">
          {t.input && <>{t.input}{'\n'}</>}
          {shownOutput && <span className="chat__tool-output">{shownOutput}</span>}
          {t.output_truncated && fullOutput === null && (
            <span className="chat__tool-output-more">
              {loadingFull
                ? '\n… loading full output …'
                : `\n… (${t.output_full_len ?? 'more'} chars total — expand to load)`}
            </span>
          )}
        </pre>
      )}
    </div>
  )
}
