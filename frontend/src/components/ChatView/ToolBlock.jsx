import { useEffect, useId, useMemo, useRef, useState } from 'react'
import Check from 'lucide-react/dist/esm/icons/check.mjs'
import Copy from 'lucide-react/dist/esm/icons/copy.mjs'
import { formatToolResult, toolResultCopyText } from './toolResultFormat.js'
import { copyPlainText } from './messageCopy.js'
import { fetchLazyText } from './lazySidecar.js'
import {
  toolActivityIcon,
  toolCallLabel,
  effectiveToolName,
} from './toolActivityLabel.js'
import { preserveTogglePosition } from './preserveTogglePosition.js'
import { ActivityTypeIcon } from './ActivityLineHeader.jsx'
import { useDisclosureState } from './disclosureState.js'

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
        {r.entries.map(({ id, key, value }) => (
          <div className="chat__tool-kv-row" key={id}>
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

export default function ToolBlock({ t, chatId, disclosureKey }) {
  // Collapsed until tapped — nothing produces a pre-opened tool block anymore
  // (the last producer, the legacy compaction path, renders as CompactionCard;
  // a legacy persisted `defaultOpen` field is ignored and renders collapsed
  // like everything else).
  const [open, setOpen] = useDisclosureState(chatId, disclosureKey)
  const headerRef = useRef(null)
  const detailRef = useRef(null)
  const headerId = useId()
  const detailId = useId()
  // Expansion fetches only the renderer-sized preview. The exact full output
  // is fetched on explicit copy and never stored in component state, so a huge
  // Read or shell result cannot inflate the transcript or retained JS heap.
  const [previewOutput, setPreviewOutput] = useState(null)
  const [previewComplete, setPreviewComplete] = useState(true)
  const [loadingPreview, setLoadingPreview] = useState(false)
  // A true 404 is terminal and explicitly degrades copying to the excerpt. A
  // network/5xx failure is retryable in place (or by closing and reopening)
  // instead of being permanently mistaken for a missing stash.
  const [missingOutput, setMissingOutput] = useState(false)
  const [loadError, setLoadError] = useState(false)
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [copyState, setCopyState] = useState('idle')
  const copyTimerRef = useRef(null)
  const copyControllerRef = useRef(null)
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
    // `loadingPreview` is intentionally not a dependency or start guard. Setting
    // it true inside this effect would otherwise re-run the effect, execute its
    // cleanup, and mark the in-flight request cancelled before the response
    // could be accepted. Closing the disclosure resets the visible loading
    // state; reopening starts a fresh request if the first one was abandoned.
    if (!open) {
      setLoadingPreview(false)
      return
    }
    // Intermediate output can be overwritten by a later aggregate. Wait for
    // the matching tool_end before reading the sidecar; the server's FIFO
    // barrier then guarantees the final queued stash wins the query.
    if (t.status === 'running') return
    if (!t.output_truncated || previewOutput !== null || missingOutput) return
    if (!chatId) return
    // Contract rule 6: a reduced block carries a stable tool_use_id and fetches
    // its full text from the side-table endpoint. Every large block is tagged
    // (card-221 migrated all history), so a block without an id has no fetchable
    // full text — leave the inline excerpt.
    if (!t.tool_use_id) return
    const url = `/chats/${chatId}/tool-output/${encodeURIComponent(t.tool_use_id)}`
      + '?preview=1'
    const controller = new AbortController()
    let cancelled = false
    setLoadingPreview(true)
    setLoadError(false)
    fetchLazyText(url, { signal: controller.signal })
      .then(({ response, text }) => {
        if (!cancelled) {
          setPreviewOutput(text)
          setPreviewComplete(response.headers.get('X-Tool-Output-Complete') !== '0')
        }
      })
      .catch(error => {
        if (cancelled) return
        if (error?.status === 404) setMissingOutput(true)
        else if (error?.name !== 'AbortError') setLoadError(true)
      })
      .finally(() => { if (!cancelled) setLoadingPreview(false) })
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [
    open,
    t.status,
    t.output_truncated,
    t.tool_use_id,
    previewOutput,
    missingOutput,
    chatId,
    loadAttempt,
  ])

  useEffect(() => {
    if (!open) {
      setPreviewOutput(null)
      setPreviewComplete(true)
      setLoadError(false)
      clearTimeout(copyTimerRef.current)
      copyControllerRef.current?.abort()
      copyControllerRef.current = null
      setCopyState('idle')
    }
  }, [open])

  // Show the larger bounded preview once it lands; until then the inline
  // excerpt remains immediately useful.
  const shownOutput = t.output_truncated && previewOutput !== null
    ? previewOutput
    : t.output

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
  const excerptOnly = !!t.output_truncated && (
    t.status === 'running'
    || missingOutput
    || !chatId
    || !t.tool_use_id
  )
  const copyLabel = excerptOnly ? 'Copy excerpt' : 'Copy output'
  const copySuccessText = excerptOnly ? 'Excerpt copied' : 'Output copied'
  const copyVisibleLabel = copyState === 'copied'
    ? 'Copied'
    : copyState === 'failed'
      ? 'Copy failed'
      : copyState === 'copying'
        ? 'Copying…'
        : copyLabel

  useEffect(() => () => {
    clearTimeout(copyTimerRef.current)
    copyControllerRef.current?.abort()
  }, [])

  async function copyOutput() {
    if (!r || copyState === 'copying') return
    clearTimeout(copyTimerRef.current)
    let output = shownOutput ?? ''
    if (
      t.output_truncated
      && t.status !== 'running'
      && !missingOutput
      && chatId
      && t.tool_use_id
      // A complete preview is already the exact output. Reuse it instead of
      // spending another request and briefly holding a duplicate string.
      && !(previewOutput !== null && previewComplete)
    ) {
      setCopyState('copying')
      const controller = new AbortController()
      copyControllerRef.current?.abort()
      copyControllerRef.current = controller
      try {
        const url = `/chats/${chatId}/tool-output/${encodeURIComponent(t.tool_use_id)}`
        const result = await fetchLazyText(url, { signal: controller.signal })
        output = result.text
      } catch (error) {
        if (error?.name === 'AbortError') return
        setCopyState('failed')
        copyTimerRef.current = setTimeout(() => setCopyState('idle'), 1800)
        return
      } finally {
        if (copyControllerRef.current === controller) copyControllerRef.current = null
      }
    }
    const copied = await copyPlainText(toolResultCopyText(output, { terminal: isShell }))
    setCopyState(copied ? 'copied' : 'failed')
    copyTimerRef.current = setTimeout(() => setCopyState('idle'), 1800)
  }

  function retryPreview() {
    setLoadError(false)
    setLoadAttempt(value => value + 1)
  }

  const showLazyStatus = !!t.output_truncated && (
    t.status === 'running'
    || loadingPreview
    || missingOutput
    || loadError
    || previewOutput === null
    || !previewComplete
  )
  let lazyStatusText = ''
  if (t.status === 'running') {
    lazyStatusText = `Showing live excerpt${
      t.output_full_len ? ` of ${t.output_full_len} characters` : ''
    }.`
  } else if (loadingPreview) {
    lazyStatusText = 'Loading output preview…'
  } else if (missingOutput) {
    lazyStatusText = 'Full output unavailable; showing excerpt.'
  } else if (loadError) {
    lazyStatusText = 'Couldn’t load output preview.'
  } else if (previewOutput !== null && !previewComplete) {
    const total = Number(t.output_full_len)
    lazyStatusText = `Showing the first ${previewOutput.length.toLocaleString()} of ${
      Number.isFinite(total) ? total.toLocaleString() : 'many'
    } characters. Copy output for the full text.`
  } else if (previewOutput === null) {
    lazyStatusText = `Showing excerpt${
      t.output_full_len ? ` of ${t.output_full_len} characters` : ''
    }.`
  }

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
          id={headerId}
          type="button"
          className="chat__tool-header"
          onClick={() => {
            preserveTogglePosition(headerRef.current, detailRef.current)
            setOpen(o => !o)
          }}
          aria-expanded={open}
          aria-controls={detailId}
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
      {hasDetail && (
        <div
          ref={detailRef}
          id={detailId}
          className="chat__tool-detail"
          role="region"
          aria-labelledby={headerId}
          tabIndex={open ? 0 : undefined}
          hidden={!open}
        >
          {open && t.input && (
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
          {open && (r || t.output_truncated) && (
            <div className="chat__tool-section">
              <div className="chat__tool-section-head">
                <span className="chat__tool-section-label">
                  {isShell ? 'Output' : 'Result'}
                </span>
                {r && (
                  <button
                    type="button"
                    className={`chat__tool-copy chat__tool-copy--${copyState}`}
                    onClick={copyOutput}
                    disabled={copyState === 'copying'}
                    aria-label={
                      copyState === 'copied'
                        ? copySuccessText
                        : copyState === 'failed'
                          ? 'Could not copy output'
                          : copyState === 'copying'
                            ? 'Copying output'
                            : copyLabel
                    }
                    title={copyState === 'failed' ? 'Try copying again' : copyLabel}
                  >
                    {copyState === 'copied'
                      ? <Check size={13} strokeWidth={2.3} aria-hidden="true" />
                      : <Copy size={13} strokeWidth={2} aria-hidden="true" />}
                    <span>{copyVisibleLabel}</span>
                  </button>
                )}
                <span className="chat__sr-only" role="status" aria-live="polite">
                  {copyState === 'copied'
                    ? copySuccessText
                    : copyState === 'failed'
                      ? 'Could not copy output'
                      : ''}
                </span>
              </div>
              {r && <ToolResult r={r} />}
              {showLazyStatus && (
                <div className="chat__tool-output-more chat__lazy-status">
                  <span
                    role={loadingPreview || missingOutput || loadError ? 'status' : undefined}
                    aria-live={loadingPreview || missingOutput || loadError ? 'polite' : undefined}
                  >
                    {lazyStatusText}
                  </span>
                  {loadError && (
                    <button
                      type="button"
                      className="chat__lazy-retry"
                      onClick={retryPreview}
                    >
                      Retry
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
