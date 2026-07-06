import { useEffect, useMemo, useState } from 'react'
import { apiFetch } from '../../api/client.js'
import { formatToolResult } from './toolResultFormat.js'

function sourceHost(url) {
  try {
    return new URL(url).host
  } catch {
    // Unparseable URLs have no meaningful host chip; the title keeps the label.
    return ''
  }
}

// Render an already-formatted tool result (see toolResultFormat.js) so shell
// output reads as a terminal (stdout / stderr / exit code) and a structured
// result reads as key/values, instead of a raw JSON blob. The formatter is pure
// and never throws; a `text` result reproduces the old plain <pre> look, so any
// unrecognized shape degrades to exactly today's rendering. `r` is passed in
// (not the raw string) so ToolBlock parses once and shares it with the header.
function ToolResult({ r }) {
  if (r.kind === 'terminal') {
    const empty = !r.stdout && !r.stderr
    return (
      <div className="chat__tool-term">
        {r.stdout && (
          <pre className="chat__tool-text chat__tool-output">{r.stdout}</pre>
        )}
        {r.stderr && (
          <pre className="chat__tool-text chat__tool-stderr">{r.stderr}</pre>
        )}
        {r.exitCode != null && r.exitCode !== 0 && (
          <span className="chat__tool-exit">exit {r.exitCode}</span>
        )}
        {/* A silent success (no stdout/stderr, exit 0) would otherwise expand to
            an empty box that reads as a bug — label it instead. */}
        {empty && (r.exitCode == null || r.exitCode === 0) && (
          <span className="chat__tool-output-more">(no output)</span>
        )}
        {r.truncated && (
          <span className="chat__tool-output-more">… output truncated</span>
        )}
      </div>
    )
  }

  if (r.kind === 'structured') {
    return (
      <div className="chat__tool-kv">
        {r.entries.map(({ key, value }) => (
          <div className="chat__tool-kv-row" key={key}>
            <span className="chat__tool-kv-key">{key}</span>
            <pre className="chat__tool-kv-val">{value}</pre>
          </div>
        ))}
        {r.truncated && (
          <span className="chat__tool-output-more">… output truncated</span>
        )}
      </div>
    )
  }

  return (
    <>
      <pre className="chat__tool-text chat__tool-output">{r.text}</pre>
      {r.truncated && (
        <span className="chat__tool-output-more">… output truncated</span>
      )}
    </>
  )
}

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
  const sources = Array.isArray(t.sources)
    ? t.sources.filter(source => source?.url) : []
  const hasSources = sources.length > 0
  const hasDetail = !!(
    t.input || t.output || t.output_truncated || hasSources
  )

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

  // Parse the shown output once — shared by the header failure chip and the
  // body renderer. A tool never carries an 'error' status (the stream only
  // moves running→done), so a nonzero exit code is the sole failure signal;
  // surface it on the header so a failed step shows without expanding.
  // Memoized on the string so a co-rendering streaming answer (which re-renders
  // this block every typewriter frame) doesn't re-JSON-parse a large output.
  const r = useMemo(
    () => (shownOutput ? formatToolResult(shownOutput) : null),
    [shownOutput],
  )
  const failed = !!(
    r && r.kind === 'terminal' && r.exitCode != null && r.exitCode !== 0
  )

  return (
    <div className={
      `chat__tool chat__tool--${t.status || 'done'}${failed ? ' chat__tool--failed' : ''}`
    }>
      <div className="chat__tool-header" onClick={() => hasDetail && setOpen(!open)}>
        {t.status === 'running' && <span className="chat__tool-spin" />}
        {/* Skill observability: when the Skill tool loaded a named
            skill, show its name as a chip so the user can see which
            skill the agent reached for this turn. */}
        {t.skill && <span className="chat__tool-chip">skill: {t.skill}</span>}
        <span className="chat__tool-name">
          {t.status === 'running' ? `Running ${toolName}...` : label}
        </span>
        {failed && (
          <span className="chat__tool-exit chat__tool-exit--head">exit {r.exitCode}</span>
        )}
        {hasDetail && <span className="chat__tool-toggle">{open ? '▾' : '▸'}</span>}
      </div>
      {open && hasDetail && (
        <div className="chat__tool-detail">
          {t.input && <pre className="chat__tool-text">{t.input}</pre>}
          {hasSources && (
            <div className="chat__tool-sources">
              {sources.map((source, i) => (
                <a
                  key={`${source.url}-${i}`}
                  className="chat__tool-source-chip"
                  href={source.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={source.snippet || source.title || source.url}
                >
                  <span className="chat__tool-source-title">
                    {source.title || source.url}
                  </span>
                  <span className="chat__tool-source-host">
                    {sourceHost(source.url)}
                  </span>
                </a>
              ))}
            </div>
          )}
          {r && <ToolResult r={r} />}
          {t.output_truncated && fullOutput === null && (
            <span className="chat__tool-output-more">
              {loadingFull
                ? '\n… loading full output …'
                : `\n… (${t.output_full_len ?? 'more'} chars total — expand to load)`}
            </span>
          )}
        </div>
      )}
    </div>
  )
}
