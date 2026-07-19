import { useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch } from '../../api/client.js'
import { formatToolResult } from './toolResultFormat.js'
import {
  toolActivityIcon,
  toolCallLabel,
  effectiveToolName,
} from './toolActivityLabel.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'
import { ActivityTypeIcon } from './ActivityLineHeader.jsx'

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
          <span className="chat__tool-output-more">No output</span>
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
      {r.text
        ? <pre className="chat__tool-text chat__tool-output">{r.text}</pre>
        : <span className="chat__tool-output-more">No output</span>}
      {r.truncated && (
        <span className="chat__tool-output-more">… output truncated</span>
      )}
    </>
  )
}

export default function ToolBlock({ t, chatId }) {
  // Collapsed until tapped — nothing produces a pre-opened tool block anymore
  // (the last producer, the legacy compaction path, renders as CompactionCard;
  // a legacy persisted `defaultOpen` field is ignored and renders collapsed
  // like everything else).
  const [open, setOpen] = useState(false)
  const headerRef = useRef(null)
  // The full output of a large tool block is fetched lazily on first expand —
  // a chat load ships only a bounded excerpt plus an output_truncated marker
  // (the write funnel reduced it and stashed the full text in tool_outputs), so
  // a Read of a huge file or a long bash run doesn't bloat the payload for
  // blocks the user never opens. Cached here so re-collapsing doesn't refetch.
  const [fullOutput, setFullOutput] = useState(null)
  const [loadingFull, setLoadingFull] = useState(false)
  // A fetch that failed (offline, or a 404 when no stash row exists) is
  // TERMINAL: without this, loadingFull flips false and the effect re-fires on
  // the same deps, retrying the fetch forever. A 404 is a designed outcome
  // (contract rule 6: keep the inline excerpt), so try once and stop.
  const [loadFailed, setLoadFailed] = useState(false)
  const effectiveName = effectiveToolName(t)
  const isShell = effectiveName === 'Bash' || effectiveName === 'shell'
  const label = toolCallLabel(t)
  const iconKind = toolActivityIcon(effectiveName)
  // `t.sources` is NOT rendered here: the turn's sources surface once at the
  // end of the message (MessageSources), where they belong to the answer
  // rather than to the one search that found them. They deliberately do not
  // make a tool row expandable on their own.
  const hasDetail = !!(t.input || t.output || t.output_truncated)

  useEffect(() => {
    // `loadingFull` is intentionally not a dependency or start guard. Setting
    // it true inside this effect would otherwise re-run the effect, execute its
    // cleanup, and mark the in-flight request cancelled before the response
    // could be accepted. Closing the disclosure resets the visible loading
    // state; reopening starts a fresh request if the first one was abandoned.
    if (!open) {
      setLoadingFull(false)
      return
    }
    if (!t.output_truncated || fullOutput !== null || loadFailed) return
    if (!chatId) return
    // Contract rule 6: a reduced block carries a stable tool_use_id and fetches
    // its full text from the side-table endpoint. Every large block is tagged
    // (card-221 migrated all history), so a block without an id has no fetchable
    // full text — leave the inline excerpt.
    if (!t.tool_use_id) return
    const url = `/chats/${chatId}/tool-output/${encodeURIComponent(t.tool_use_id)}`
    let cancelled = false
    setLoadingFull(true)
    apiFetch(url)
      .then(res => (res.ok ? res.text() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then(text => { if (!cancelled) setFullOutput(text) })
      .catch(() => { if (!cancelled) setLoadFailed(true) })
      .finally(() => { if (!cancelled) setLoadingFull(false) })
    return () => { cancelled = true }
  }, [open, t.output_truncated, t.tool_use_id, fullOutput, loadFailed, chatId])

  // Show the fetched full output once it lands; until then the inline preview.
  const shownOutput = t.output_truncated && fullOutput !== null ? fullOutput : t.output

  // Parse the shown output once — shared by the header failure chip and the
  // body renderer. A tool never carries an 'error' status (the stream only
  // moves running→done), so a nonzero exit code is the sole failure signal;
  // surface it on the header so a failed step shows without expanding.
  // Memoized on the string so a co-rendering streaming answer (which re-renders
  // this block every typewriter frame) doesn't re-JSON-parse a large output.
  // Live tool items start with output: ''. That is "not emitted yet", not a
  // silent success, so only turn an empty string into "No output" after the
  // step settles. Non-empty streaming output remains inspectable immediately.
  const hasOutput = !!shownOutput
    || !!t.output_truncated
    || (t.status !== 'running' && shownOutput === '')
  const r = useMemo(
    () => (hasOutput
      ? formatToolResult(shownOutput ?? '', { terminal: isShell })
      : null),
    [shownOutput, hasOutput, isShell],
  )
  // Failure exit code, field-or-parse (contract rule 6): a block reduced at the
  // funnel carries an explicit output_exit_code, so read that rather than
  // re-parsing a possibly-carved excerpt; else fall back to the parsed terminal
  // envelope. This surfaces a failed step on the collapsed header without a
  // fetch, even when the inline text is only an excerpt.
  const exitCode = t.output_exit_code != null
    ? t.output_exit_code
    : (r && r.kind === 'terminal' ? r.exitCode : null)
  const failed = exitCode != null && exitCode !== 0

  // The header content is shared by both shells below so the visual row is
  // identical whether or not it is interactive.
  const headerContent = (
    <>
      <span
        className={`chat__tool-icon${t.status === 'running' ? ' chat__tool-icon--running' : ''}`}
        data-tool-kind={iconKind}
        aria-hidden="true"
      >
        <ActivityTypeIcon kind={iconKind} />
      </span>
      {/* Skill observability: when the Skill tool loaded a named
          skill, show its name as a chip so the user can see which
          skill the agent reached for this turn. */}
      {t.skill && <span className="chat__tool-chip">skill: {t.skill}</span>}
      {/* The group header names the category ("Ran commands"); each child row
          names the concrete operation ("Ran git status -sb"). */}
      <span className="chat__tool-name" title={label}>
        {label}{t.status === 'running' ? '…' : ''}
      </span>
      {failed && (
        <span className="chat__tool-exit chat__tool-exit--head">exit {exitCode}</span>
      )}
    </>
  )

  return (
    <div className={
      `chat__tool chat__tool--${t.status || 'done'}${failed ? ' chat__tool--failed' : ''}`
    }>
      {hasDetail ? (
        // A real <button> so the disclosure is keyboard-operable (the old
        // clickable <div> was not); the toggle logic is otherwise unchanged.
        <button
          ref={headerRef}
          type="button"
          className="chat__tool-header"
          onClick={() => {
            preserveTogglePosition(headerRef.current)
            setOpen(o => !o)
          }}
          aria-expanded={open}
        >
          {headerContent}
        </button>
      ) : (
        // Nothing to inspect — a static, non-interactive row (no toggle, no
        // keyboard affordance) so it doesn't read as a dead button.
        <div className="chat__tool-header chat__tool-header--static">
          {headerContent}
        </div>
      )}
      {open && hasDetail && (
        <div className="chat__tool-detail">
          {t.input && (
            <div className="chat__tool-section">
              <span className="chat__tool-section-label">
                {isShell ? 'Command' : 'Input'}
              </span>
              <pre className={
                `chat__tool-text${isShell ? ' chat__tool-command' : ''}`
              }>
                {isShell && <span className="chat__tool-prompt" aria-hidden="true">$ </span>}
                {t.input}
              </pre>
            </div>
          )}
          {(r || t.output_truncated) && (
            <div className="chat__tool-section">
              <span className="chat__tool-section-label">
                {isShell ? 'Output' : 'Result'}
              </span>
              {r && <ToolResult r={r} />}
              {t.output_truncated && fullOutput === null && (
                <span className="chat__tool-output-more">
                  {loadingFull
                    ? '… loading full output …'
                    : loadFailed
                      ? '… full output unavailable; showing excerpt'
                      : `… showing excerpt${t.output_full_len ? ` of ${t.output_full_len} characters` : ''}`}
                </span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
