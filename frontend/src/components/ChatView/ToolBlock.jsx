import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { Check, Copy } from '@openai/apps-sdk-ui/components/Icon'
import {
  formatToolResult,
  toolBlockFailed,
  toolResultCopyText,
} from './toolResultFormat.js'
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
import MemoryRecallCard from './MemoryRecallCard.jsx'
import ToolImageResult from './ToolImageResult.jsx'
import {
  servedImageReference,
  toolImageReference,
} from './toolImageResult.js'
import {
  pointerSelectionChangedWithin,
  textSelectionSnapshot,
} from '../../lib/selectableTextControl.js'
import { useToolImagePreview } from './useToolImagePreview.js'
import ToolEditPreview from './ToolEditPreview.jsx'
import { toolEditPreview } from './toolEditPreview.js'

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

function GenericToolBlock({ t, chatId, compact = false, disclosureKey }) {
  // Collapsed until tapped — nothing produces a pre-opened tool block anymore
  // (the last producer, the legacy compaction path, renders as CompactionCard;
  // a legacy persisted `defaultOpen` field is ignored and renders collapsed
  // like everything else).
  const [desiredOpen, setDesiredOpen] = useDisclosureState(chatId, disclosureKey)
  const headerRef = useRef(null)
  const detailRef = useRef(null)
  const desiredOpenRef = useRef(desiredOpen)
  const visibleOpenRef = useRef(false)
  const headerId = useId()
  const detailId = useId()
  // Pointer/keyboard activation prepares only the row the owner chose. If the
  // load crosses the click boundary, desiredOpen records the request while the
  // rendered disclosure waits for its first complete layout.
  const [prepareRequested, setPrepareRequested] = useState(desiredOpen)
  // Expansion normally fetches only the renderer-sized preview. The exact full
  // output is fetched on explicit copy and never stored in component state, so
  // a huge Read or shell result cannot inflate the transcript or retained JS
  // heap. The one exception is an image viewed outside chat-owned media: its
  // complete base64 envelope is required to build the visual fallback.
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
  const pointerSelectionRef = useRef(null)
  const effectiveName = effectiveToolName(t)
  // Skill reads keep the raw owning tool beneath their plain-language header.
  // Use that raw identity for command/result formatting even though
  // effectiveToolName intentionally classifies the collapsed row as Skill.
  const isShell = t?.tool === 'Bash' || t?.tool === 'shell'
  const label = toolCallLabel(t)
  const iconKind = toolActivityIcon(effectiveName)
  const isImageTool = effectiveName === 'ViewImage'
  const hasEditPreview = typeof t.edit_preview?.diff === 'string'
  const failed = toolBlockFailed(t)
  // Historical activities can contain many closed edits. Keep the durable
  // marker cheap and defer parsing until this disclosure is prepared.
  const wantsPreparation = prepareRequested || desiredOpen
  const editPreview = useMemo(
    () => (wantsPreparation && !failed ? toolEditPreview(t.edit_preview) : null),
    [failed, t.edit_preview, wantsPreparation],
  )
  const servedImage = useMemo(() => (
    isImageTool ? servedImageReference(t.input, chatId) : null
  ), [isImageTool, t.input, chatId])
  // `t.sources` is NOT rendered here: the turn's sources surface once at the
  // end of the message (MessageSources), where they belong to the answer
  // rather than to the one search that found them. They deliberately do not
  // make a tool row expandable on their own.
  const skillNames = effectiveName === 'Skill' && Array.isArray(t.skills)
    ? t.skills.filter(skill => typeof skill === 'string' && skill.trim())
    : []
  const hasDetail = !!(
    t.input || t.output || t.output_truncated || hasEditPreview || skillNames.length > 1
  )

  useEffect(() => {
    // `loadingPreview` is intentionally not a dependency or start guard. Setting
    // it true inside this effect would otherwise re-run the effect, execute its
    // cleanup, and mark the in-flight request cancelled before the response
    // could be accepted. Closing the disclosure resets the visible loading
    // state; reopening starts a fresh request if the first one was abandoned.
    if (!prepareRequested) {
      setLoadingPreview(false)
      return
    }
    // Intermediate output can be overwritten by a later aggregate. Wait for
    // the matching tool_end before reading the sidecar; the server's FIFO
    // barrier then guarantees the final queued stash wins the query.
    if (t.status === 'running') return
    if (!t.output_truncated || previewOutput !== null || missingOutput) return
    // Protected chat media and /tmp rasters render through narrow routes,
    // avoiding the image tool's much larger base64 sidecar. An image viewed
    // elsewhere needs the complete result (not the ordinary 20k text preview)
    // so the fallback data URL is valid.
    if (isImageTool && servedImage) return
    if (!chatId) return
    // Contract rule 6: a reduced block carries a stable tool_use_id and fetches
    // its full text from the side-table endpoint. Every large block is tagged
    // (card-221 migrated all history), so a block without an id has no fetchable
    // full text — leave the inline excerpt.
    if (!t.tool_use_id) return
    const url = `/chats/${chatId}/tool-output/${encodeURIComponent(t.tool_use_id)}`
      + (isImageTool ? '' : '?preview=1')
    const controller = new AbortController()
    let cancelled = false
    setLoadingPreview(true)
    setLoadError(false)
    fetchLazyText(url, { signal: controller.signal })
      .then(({ response, text }) => {
        if (!cancelled) {
          setLoadingPreview(false)
          // A text preview is the last missing layout input for an ordinary
          // tool. Viewed images still need to decode before their body can be
          // revealed, so their image-preparation hook owns that boundary.
          if (!isImageTool) revealBeforeReady()
          setPreviewOutput(text)
          setPreviewComplete(response.headers.get('X-Tool-Output-Complete') !== '0')
        }
      })
      .catch(error => {
        if (cancelled) return
        if (error?.status === 404) {
          setLoadingPreview(false)
          if (!isImageTool) revealBeforeReady()
          setMissingOutput(true)
        } else if (error?.name !== 'AbortError') {
          setLoadingPreview(false)
          if (!isImageTool) revealBeforeReady()
          setLoadError(true)
        }
      })
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [
    prepareRequested,
    t.status,
    t.output_truncated,
    t.tool_use_id,
    previewOutput,
    missingOutput,
    chatId,
    loadAttempt,
    isImageTool,
    servedImage,
  ])

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
  const imageReference = useMemo(
    () => (isImageTool ? toolImageReference(t.input, shownOutput, chatId) : null),
    [isImageTool, shownOutput, t.input, chatId],
  )
  const r = useMemo(
    () => (hasOutput && !isImageTool
      ? formatToolResult(shownOutput ?? '', { terminal: isShell })
      : null),
    [shownOutput, hasOutput, isShell, isImageTool],
  )
  const waitsForPreview = !!(
    t.output_truncated
    && t.status !== 'running'
    && !servedImage
    && previewOutput === null
    && !missingOutput
    && !loadError
    && chatId
    && t.tool_use_id
  )
  const previewReady = !waitsForPreview
  const imagePreview = useToolImagePreview(imageReference, {
    enabled: isImageTool && wantsPreparation && previewReady,
    onSettled: revealBeforeReady,
  })
  const imageReady = !isImageTool || (
    imageReference
      ? imagePreview.status === 'ready' || imagePreview.status === 'failed'
      : previewReady
  )
  const detailReady = previewReady && imageReady
  const open = desiredOpen && detailReady
  const opening = desiredOpen && !open
  // A successful edit preview already owns the complete disclosure: its file
  // headers name every path and its diff proves what changed. Generic Input
  // and Result sections would repeat the first path plus a provider-specific
  // success sentence/model repr. Failed or unpreviewable edits keep those
  // diagnostics because editPreview is deliberately absent for them.
  const showGenericInput = !!(open && t.input && !isImageTool && !editPreview)
  const showGenericResult = !!(
    open && !editPreview && (r || t.output_truncated || isImageTool)
  )
  desiredOpenRef.current = desiredOpen
  visibleOpenRef.current = open
  // Failure exit code, field-or-parse (contract rule 6): a block reduced at the
  // funnel carries an explicit output_exit_code, so read that rather than
  // re-parsing a possibly-carved excerpt; else fall back to the parsed terminal
  // envelope. This surfaces a failed step on the collapsed header without a
  // fetch, even when the inline text is only an excerpt.
  const exitCode = t.output_exit_code != null
    ? t.output_exit_code
    : (r && r.kind === 'terminal' ? r.exitCode : null)
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

  function revealBeforeReady() {
    if (!desiredOpenRef.current || visibleOpenRef.current) return
    preserveTogglePosition(headerRef.current, detailRef.current)
  }

  function releaseClosedDetail() {
    setPreviewOutput(null)
    setPreviewComplete(true)
    setLoadingPreview(false)
    setLoadError(false)
    clearTimeout(copyTimerRef.current)
    copyControllerRef.current?.abort()
    copyControllerRef.current = null
    setCopyState('idle')
  }

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
    if (open) preserveTogglePosition(headerRef.current, detailRef.current)
    setLoadError(false)
    setLoadAttempt(value => value + 1)
  }

  const showLazyStatus = !!t.output_truncated && !servedImage && (
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
      {/* The group header names the category ("Ran commands"); each child row
          names the concrete operation ("Ran git status -sb"). */}
      <span className="chat__tool-name" title={label}>
        {label}{t.status === 'running' || opening ? '…' : ''}
      </span>
      {/* A direct compact row IS the collapsed transcript overview, so its
          technical code waits inside the disclosed result. A grouped child is
          already behind the activity disclosure and can carry the diagnostic
          here for quick scanning. */}
      {failed && !compact && (
        <span className="chat__tool-exit chat__tool-exit--head">exit {exitCode}</span>
      )}
    </>
  )

  return (
    <div className={
      `chat__tool chat__tool--${t.status || 'done'}${failed ? ' chat__tool--failed' : ''}`
      + (compact ? ' chat__tool--compact' : '')
      + (isImageTool ? ' chat__tool--image' : '')
    }>
      {hasDetail ? (
        // A real <button> so the disclosure is keyboard-operable (the old
        // clickable <div> was not); the toggle logic is otherwise unchanged.
        <button
          ref={headerRef}
          id={headerId}
          type="button"
          className="chat__tool-header"
          onPointerDown={() => {
            pointerSelectionRef.current = textSelectionSnapshot()
            setPrepareRequested(true)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              setPrepareRequested(true)
            }
          }}
          onClick={(event) => {
            const selectionBeforePointer = pointerSelectionRef.current
            pointerSelectionRef.current = null
            if (
              event.detail !== 0
              && pointerSelectionChangedWithin(
                selectionBeforePointer,
                headerRef.current,
              )
            ) {
              setPrepareRequested(false)
              releaseClosedDetail()
              return
            }
            const nextOpen = !desiredOpen
            if (nextOpen) {
              setPrepareRequested(true)
              if (detailReady) {
                preserveTogglePosition(headerRef.current, detailRef.current)
              }
            } else {
              if (open) preserveTogglePosition(headerRef.current, detailRef.current)
              setPrepareRequested(false)
              releaseClosedDetail()
            }
            desiredOpenRef.current = nextOpen
            setDesiredOpen(nextOpen)
          }}
          aria-expanded={open}
          aria-busy={opening || undefined}
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
          className={`chat__tool-detail${
            editPreview ? ' chat__tool-detail--edit' : ''
          }`}
          data-chat-scroll-region
          role="region"
          aria-labelledby={headerId}
          tabIndex={open ? 0 : undefined}
          hidden={!open}
        >
          {open && skillNames.length > 1 && (
            <div className="chat__tool-section">
              <span className="chat__tool-section-label">Skills</span>
              <ul className="chat__skill-list">
                {skillNames.map(skill => <li key={skill}>{skill}</li>)}
              </ul>
            </div>
          )}
          {showGenericInput && (
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
          {open && editPreview && <ToolEditPreview preview={editPreview} />}
          {showGenericResult && (
            <div className={isImageTool ? 'chat__tool-image-result' : 'chat__tool-section'}>
              {!isImageTool && (
                <div className="chat__tool-section-head">
                  <span className="chat__tool-section-label">
                    {isShell ? 'Output' : 'Result'}
                  </span>
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
                      ? <Check width={13} height={13} aria-hidden="true" />
                      : <Copy width={13} height={13} aria-hidden="true" />}
                    <span>{copyVisibleLabel}</span>
                  </button>
                  <span className="chat__sr-only" role="status" aria-live="polite">
                    {copyState === 'copied'
                      ? copySuccessText
                      : copyState === 'failed'
                        ? 'Could not copy output'
                        : ''}
                  </span>
                </div>
              )}
              {imageReference
                ? (
                  <ToolImageResult
                    reference={imageReference}
                    preview={imagePreview}
                  />
                )
                : isImageTool && !showLazyStatus
                  ? (
                    <span className="chat__tool-image-status" role="status">
                      Image preview unavailable
                    </span>
                  )
                  : r && <ToolResult r={r} />}
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

export default function ToolBlock({
  t,
  chatId,
  compact = false,
  disclosureKey,
  onInternalNav,
}) {
  if (effectiveToolName(t) === 'MemoryRecall') {
    return (
      <MemoryRecallCard
        t={t}
        chatId={chatId}
        disclosureKey={disclosureKey}
        onInternalNav={onInternalNav}
      />
    )
  }
  return (
    <GenericToolBlock
      t={t}
      chatId={chatId}
      compact={compact}
      disclosureKey={disclosureKey}
    />
  )
}
