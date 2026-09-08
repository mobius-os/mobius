/* Complete chat-scoped contribution control surface. */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { X } from '@openai/apps-sdk-ui/components/Icon'
import useContextMenuOutsideDismiss from '../../hooks/useContextMenuOutsideDismiss.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { formatRelativeTime } from '../../lib/relativeTime.js'
import FileDiffList from '../DiffView/FileDiffList.jsx'
import ChatContributionDiff from './ChatContributionDiff.jsx'
import {
  chatContributionOutcomeActions,
  chatChangesPrimaryAction,
  contributionActionOutcome,
  contributionWorkContext,
  preparedChangesPrimaryAction,
} from './chatContributionIntent.js'
import {
  contributionNeedsAttention,
  contributionStage,
  contributionWorkState,
  groupUnsortedFiles,
} from './chatChangesLifecycle.js'
import {
  contributionReviewIntent,
  currentReviewItems,
  publicationAction,
  publicationFailureOwner,
  publicationItemsAction,
  publicationItemsMutations,
  publicationStackAction,
  reviewItems,
  refreshedReviewItems,
  sendBlocker,
  stackPublicationRecords,
  stackSendBlocker,
} from './contributionReviewModel.js'
import { useChatChangesOverview } from './useChatChangesOverview.js'
import {
  projectPublishedContribution,
  publishContribution,
  publishContributionStack,
} from './chatContributionPublication.js'
import './ChatWork.css'

const SURFACE_STAGES = ['working', 'ready', 'needs_you', 'done']

const STAGE_LABELS = {
  working: 'Working',
  ready: 'Ready',
  needs_you: 'Needs you',
  done: 'Done',
}

function updateTime(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return formatRelativeTime(new Date(value).toISOString())
  }
  return formatRelativeTime(value)
}

function lifecycleStatus(record, stage) {
  if (record?.kind === 'local') return 'Kept local'
  if (contributionNeedsAttention(record)) return 'Needs attention'
  if (record?.status === 'submitting') return 'Publishing'
  if (record?.status === 'draft') return 'Draft PR'
  if (record?.status === 'landing') return 'Merging'
  if (record?.status === 'superseded') return 'Already shared'
  if (record?.status === 'closed') return 'Closed, not merged'
  if (stage === 'prepared') return 'Private review'
  if (stage === 'open') return 'Sent'
  return 'Merged'
}

function recordRevision(record) {
  return `${record?.id || ''}:${record?.action_key || record?.updated_at || ''}:${record?.status || ''}`
}

function preparedItemRevision(item) {
  if (item?.kind === 'record') return recordRevision(item.record)
  return `${item?.id || ''}:${(item?.records || []).map(recordRevision).join('|')}`
}

function localDispositionLabel(record) {
  return {
    'local-only': 'Local to this instance',
    personal: 'Personal work',
    experimental: 'Experimental work',
    'incoming-only': 'Incoming work',
    duplicate: 'Already covered elsewhere',
  }[record?.disposition] || 'Kept local'
}

function preparedRepresentative(item, records = []) {
  if (item?.kind !== 'stack') return item?.record || null
  return item.records.find(record => records.some(row => row.id === record.id))
    || item.records.at(-1)
    || null
}

function preparedTitle(item, record) {
  return item?.kind === 'stack'
    ? item.stack?.name || record?.summary || 'Linked contribution'
    : record?.summary || record?.title || 'Contribution from this chat'
}

function EmptyStage({ stage, hasRecordedEdits }) {
  const copy = {
    working: hasRecordedEdits
      ? ['Everything is organized', 'Every recorded edit is covered by prepared or public work.']
      : ['No file changes yet', 'Edits made through this chat will collect here automatically.'],
    ready: ['Nothing ready', 'Private reviews created from this chat will appear here with their exact diffs.'],
    needs_you: ['Nothing needs you', 'Automatic checks and preparation can keep moving without an owner decision.'],
    done: ['Nothing finished yet', 'Merged, closed, and intentionally local work will collect here.'],
  }[stage]
  return (
    <div className="chat-work__empty">
      <strong>{copy[0]}</strong>
      <span>{copy[1]}</span>
    </div>
  )
}

function preparationStatusView(work, state) {
  const waitingForSource = state === 'active' && work?.status === 'accepted'
  const retryingStart = state === 'active' && work?.status === 'retrying'
  return {
    active: {
      title: retryingStart
        ? 'Retrying preparation'
        : waitingForSource
        ? 'Waiting for the edit to settle'
        : 'Preparing changes',
      copy: retryingStart
        ? String(work?.result || '').trim()
          || 'The first start did not complete. Möbius will retry it automatically.'
        : waitingForSource
        ? 'Your request is saved. It starts after the current reply, then continues in the background. You can close Changes.'
        : 'One compact helper is aligning this batch. This chat stays available, and the stages below update as it settles work.',
    },
    attention: {
      title: 'Preparation needs another pass',
      copy: String(work?.result || '').trim()
        || 'Choose the current contribution action below to try again against the latest source.',
    },
  }[state]
}

export default function ChatDiffViewer({
  chatId,
  initialEntries,
  onClose,
  onPrepareChanges,
  onContributeAll,
  onCheckUpdates,
  onOpenApp,
  onContinueInChat,
  onStopWork,
  returnFocusRef,
}) {
  const queryClient = useQueryClient()
  const overview = useChatChangesOverview(chatId, initialEntries)
  const work = overview.work || null
  const workState = contributionWorkState(work)
  const workContext = contributionWorkContext(overview)
  const workActive = workState === 'active'
  const publicationPending = overview.stages.prepared.some(
    record => record?.status === 'submitting' && record?.successor !== true,
  )
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const restoreFocusGuardRef = useRef(true)
  const expansionSequenceRef = useRef(0)
  const stageSeededRef = useRef(false)
  const [activeStage, setActiveStage] = useState('working')
  const [expansionCommand, setExpansionCommand] = useState(null)
  const [accepted, setAccepted] = useState(() => new Set())
  const [failures, setFailures] = useState({})
  const [confirming, setConfirming] = useState(null)
  const [confirmationNotice, setConfirmationNotice] = useState('')
  const [publishPhase, setPublishPhase] = useState(null)
  const [helperStarting, setHelperStarting] = useState(false)
  const [helperStartError, setHelperStartError] = useState('')
  const [helperStopping, setHelperStopping] = useState(false)
  const [helperStopError, setHelperStopError] = useState('')
  const helperInFlightRef = useRef(false)
  const retryHelperRef = useRef(null)
  const helperStopInFlightRef = useRef(false)
  const publishInFlightRef = useRef(false)

  const dismissFromOutside = useCallback(() => {
    // The outside press belongs to the destination beneath this
    // pointer-transparent panel. Do not pull focus back to the Changes trigger
    // after that destination has already started taking ownership.
    restoreFocusGuardRef.current = false
    onClose?.()
  }, [onClose])
  const shouldRestoreFocus = useCallback(
    () => restoreFocusGuardRef.current !== false,
    [],
  )

  useContextMenuOutsideDismiss({
    open: true,
    menuRef: dialogRef,
    onDismiss: dismissFromOutside,
  })

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: closeRef,
    restoreFocusRef: returnFocusRef,
    shouldRestoreFocus,
    onClose,
    modal: false,
    lockScroll: false,
  })

  useEffect(() => {
    if (overview.loading || stageSeededRef.current) return
    stageSeededRef.current = true
    const prepared = overview.stages.prepared || []
    const needsAttention = [
      ...prepared,
      ...(overview.stages.open || []),
      ...(overview.stages.settled || []),
    ].filter(contributionNeedsAttention).length
    if (needsAttention > 0) setActiveStage('needs_you')
    else if (prepared.length > 0) setActiveStage('ready')
    else if (overview.counts.unsorted > 0 || (overview.stages.open || []).length > 0) setActiveStage('working')
    else setActiveStage('done')
  }, [overview, workState])

  const previousWorkRef = useRef({ id: work?.id || '', state: workState })
  useEffect(() => {
    const previous = previousWorkRef.current
    const current = { id: work?.id || '', state: workState }
    if (previous.id !== current.id || previous.state !== current.state) {
      setHelperStartError('')
      setHelperStopError('')
    }
    previousWorkRef.current = current
  }, [work?.id, workState])

  const unsortedGroups = useMemo(
    () => groupUnsortedFiles(overview.unsortedFiles),
    [overview.unsortedFiles],
  )
  const visiblePrepared = overview.stages.prepared.filter(
    record => !accepted.has(recordRevision(record)),
  )
  const readyRecords = visiblePrepared.filter(record => !contributionNeedsAttention(record))
  const visiblePreparedItems = reviewItems({
    ...(overview.contributions || {}),
    records: readyRecords,
  })
  const attentionRecords = [
    ...visiblePrepared,
    ...overview.stages.open,
    ...overview.stages.settled,
  ].filter(contributionNeedsAttention)
  const workingRecords = overview.stages.open.filter(
    record => !contributionNeedsAttention(record),
  )
  const doneRecords = [
    ...overview.stages.settled,
  ].filter(record => !contributionNeedsAttention(record))
  const visibleRecords = activeStage === 'working'
    ? workingRecords
    : activeStage === 'needs_you'
    ? attentionRecords
    : activeStage === 'done'
      ? doneRecords
      : []
  // A completed prepare/finish that produced no private review and opened no
  // pull request, while edits still wait unsorted, did not advance: its result
  // is a decision the owner must act on (commonly an unshared dependency), not
  // silent success. Surface that reason instead of silently re-offering the
  // same outcome buttons.
  const workDecisionResult = workState === 'completed'
    ? String(work?.result || '').trim()
    : ''
  const workDecision = (
    workDecisionResult
    && overview.stages.prepared.length === 0
    && overview.stages.open.length === 0
    && (overview.counts.unsorted || 0) > 0
  ) ? { result: workDecisionResult } : null
  const surfaceCounts = {
    working: overview.counts.unsorted + workingRecords.length,
    ready: readyRecords.length,
    needs_you: attentionRecords.length + (workDecision ? 1 : 0),
    done: doneRecords.length,
  }
  const [selectedPreparedKey, setSelectedPreparedKey] = useState('')
  useEffect(() => {
    if (activeStage !== 'ready') return
    const current = visiblePreparedItems.some(item => preparedItemRevision(item) === selectedPreparedKey)
    if (!current) setSelectedPreparedKey(
      visiblePreparedItems[0] ? preparedItemRevision(visiblePreparedItems[0]) : '',
    )
  }, [activeStage, selectedPreparedKey, visiblePreparedItems])
  const selectedPreparedItem = visiblePreparedItems.find(
    item => preparedItemRevision(item) === selectedPreparedKey,
  ) || visiblePreparedItems[0] || null
  const shortenedCount = overview.unsortedEntries.filter(entry => entry.preview?.truncated).length
  const lifecycleAction = chatChangesPrimaryAction(overview)
  const preparedPrimaryAction = preparedChangesPrimaryAction(visiblePreparedItems, {
    connected: overview.contributions?.connected !== false,
  })
  const outcomeActions = chatContributionOutcomeActions(overview, preparedPrimaryAction)
  const preparationView = preparationStatusView(work, workState)
  const confirmingAction = confirming ? publicationItemsAction(confirming) : null
  const confirmingMutations = confirming ? publicationItemsMutations(confirming) : []

  function consume(record) {
    setAccepted(current => new Set(current).add(recordRevision(record)))
  }

  function release(record, failure) {
    const key = recordRevision(record)
    setAccepted(current => {
      const next = new Set(current)
      next.delete(key)
      return next
    })
    setFailures(current => ({ ...current, [record.id]: failure }))
  }

  function setEveryDiffExpanded(expanded) {
    expansionSequenceRef.current += 1
    setExpansionCommand({ id: expansionSequenceRef.current, expanded })
  }

  function selectStageFromKeyboard(event, stage) {
    const currentIndex = SURFACE_STAGES.indexOf(stage)
    let nextIndex = currentIndex
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (currentIndex + 1) % SURFACE_STAGES.length
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (currentIndex - 1 + SURFACE_STAGES.length) % SURFACE_STAGES.length
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = SURFACE_STAGES.length - 1
    else return
    event.preventDefault()
    const nextStage = SURFACE_STAGES[nextIndex]
    setActiveStage(nextStage)
    requestAnimationFrame(() => document.getElementById(`chat-work-tab-${nextStage}`)?.focus())
  }

  function openContribute(record = null, intent = '') {
    const resolvedIntent = intent || contributionReviewIntent(record) || 'reviews:queue'
    if (!overview.contributeApp || !onOpenApp) return
    onOpenApp(overview.contributeApp, { final: true, intent: resolvedIntent })
  }

  async function requestHelper(callback) {
    if (helperInFlightRef.current) return null
    helperInFlightRef.current = true
    retryHelperRef.current = callback
    setHelperStartError('')
    setHelperStarting(true)
    const outcome = await contributionActionOutcome(callback)
    setHelperStarting(false)
    helperInFlightRef.current = false
    if (outcome.kind === 'unavailable') {
      setHelperStartError(
        'The contribution helper is not available yet. Your work is unchanged; try again after it is available.',
      )
    } else if (outcome.kind === 'blocked') {
      setHelperStartError(
        outcome.message || 'This contribution action is not available in its current state.',
      )
    } else if (outcome.kind === 'accepted' || outcome.kind === 'refreshed') {
      retryHelperRef.current = null
    }
    return outcome.kind === 'accepted'
  }

  function retryHelper() {
    if (typeof retryHelperRef.current !== 'function') return
    void requestHelper(retryHelperRef.current)
  }

  function beginConfirmation(items) {
    setConfirmationNotice('')
    setConfirming(items)
  }

  async function stopHelper(workItem) {
    if (helperStopInFlightRef.current || typeof onStopWork !== 'function') return
    helperStopInFlightRef.current = true
    setHelperStopError('')
    setHelperStopping(true)
    const outcome = await contributionActionOutcome(() => onStopWork(workItem, workContext))
    setHelperStopping(false)
    helperStopInFlightRef.current = false
    if (outcome.kind !== 'accepted' && outcome.kind !== 'refreshed') {
      setHelperStopError(
        outcome.message
          || 'Preparation could not be stopped yet. It is still safe to try again.',
      )
    }
  }

  async function continueInChat(record) {
    const acceptedRequest = await requestHelper(
      () => onContinueInChat?.(record, workContext),
    )
    if (acceptedRequest === true) {
      consume(record)
    }
  }

  function reviewContributionOutcome() {
    setEveryDiffExpanded(false)
    if (visiblePreparedItems.length > 0) setActiveStage('ready')
    else if (attentionRecords.length > 0) setActiveStage('needs_you')
    else setActiveStage('working')
  }

  async function prepareContributionOutcome() {
    if (!outcomeActions || outcomeActions.prepare.disabled) return
    if (lifecycleAction?.kind === 'prepare') {
      await requestHelper(
        () => onPrepareChanges?.(overview.unsortedRevision, workContext),
      )
      return
    }
    if (lifecycleAction?.kind === 'updates') {
      await requestHelper(
        () => onCheckUpdates?.(overview.stages.open, workContext),
      )
      return
    }
    await requestHelper(
      () => onContributeAll?.(overview.workflowRevision, workContext),
    )
  }

  async function mergeContributionOutcome() {
    if (!outcomeActions) return
    if (lifecycleAction?.kind === 'prepare') {
      await requestHelper(
        () => onPrepareChanges?.(overview.unsortedRevision, workContext),
      )
      return
    }
    if (lifecycleAction?.kind === 'finish' || preparedPrimaryAction?.kind === 'fix-prepared') {
      await requestHelper(
        () => onContributeAll?.(overview.workflowRevision, workContext),
      )
      return
    }
    if (preparedPrimaryAction?.kind === 'publish-items') {
      beginConfirmation(preparedPrimaryAction.items)
      return
    }
    if (lifecycleAction?.kind === 'updates') {
      await requestHelper(
        () => onCheckUpdates?.(overview.stages.open, workContext),
      )
    }
  }

  async function publish(record) {
    consume(record)
    setFailures(current => ({ ...current, [record.id]: null }))
    const outcome = await publishContribution({
      appId: overview.contributeAppId,
      record,
      autopilot: overview.contributions?.autopilot_available === true
        && overview.contributions?.autopilot_default !== false,
      refetch: overview.contributionsQuery.refetch,
    })
    if (outcome.kind === 'published') {
      queryClient.setQueryData(overview.contributionsQuery.queryKey, current => (
        projectPublishedContribution(current, record.id, outcome.publication)
      ))
      return { ok: true }
    }
    if (outcome.kind === 'reconciled') {
      return { ok: true, reconciled: true }
    }
    if (outcome.kind === 'pending') {
      release(record, null)
      return { ok: false, pending: true }
    }
    if (publicationFailureOwner(outcome.failure) === 'owner') {
      release(record, outcome.failure)
      return { ok: false, owner: true }
    }
    return { ok: false, recover: outcome.record, failure: outcome.failure }
  }

  function consumeItem(item) {
    if (item?.kind === 'stack') stackPublicationRecords(item).forEach(consume)
    else if (item?.record) consume(item.record)
  }

  function releaseItem(item, failure) {
    if (item?.kind === 'stack') {
      stackPublicationRecords(item).forEach(record => release(record, failure))
    }
    else if (item?.record) release(item.record, failure)
  }

  async function publishStack(item) {
    consumeItem(item)
    const outcome = await publishContributionStack({
      appId: overview.contributeAppId,
      item,
      refetch: overview.contributionsQuery.refetch,
    })
    if (outcome.kind === 'published' || outcome.kind === 'reconciled') {
      return { ok: true }
    }
    if (outcome.kind === 'pending') {
      releaseItem(item, null)
      return { ok: false, pending: true }
    }
    if (publicationFailureOwner(outcome.failure) === 'owner') {
      releaseItem(item, outcome.failure)
      return { ok: false, owner: true }
    }
    return { ok: false, recover: true, failure: outcome.failure }
  }

  async function publishBatch(items) {
    if (publishInFlightRef.current || helperInFlightRef.current) return
    publishInFlightRef.current = true
    setPublishPhase('checking')
    const refreshed = await overview.contributionsQuery.refetch().catch(() => null)
    if (!refreshed?.data) {
      setConfirmationNotice('Could not refresh the reviewed set. Check your connection and try again; nothing was sent.')
      setPublishPhase(null)
      publishInFlightRef.current = false
      return
    }
    const current = currentReviewItems(items, refreshed.data)
    if (!current) {
      const latest = refreshedReviewItems(items, refreshed.data)
      setConfirming(latest.length > 0 ? latest : null)
      setConfirmationNotice(latest.length > 0
        ? 'The reviewed set changed. The current actions are listed now; confirm this refreshed set when you are ready.'
        : 'Those actions have already moved on. Nothing was sent.')
      setPublishPhase(null)
      publishInFlightRef.current = false
      return
    }
    setPublishPhase('publishing')
    const outcomes = []
    for (const item of current) {
      outcomes.push(item.kind === 'stack'
        ? await publishStack(item)
        : await publish(item.record))
    }
    const recoveries = outcomes
      .map((outcome, index) => outcome?.recover ? [current[index], outcome] : null)
      .filter(Boolean)
    if (recoveries.length > 0) {
      // The requests above already reconciled the ledger. One batch has one
      // recovery intent, based on current state rather than the stale click.
      setPublishPhase('recovering')
      const acceptedRequest = await requestHelper(
        () => onContributeAll?.(overview.workflowRevision, workContext),
      )
      if (acceptedRequest !== true) {
        recoveries.forEach(([item, outcome]) => releaseItem(item, outcome.failure))
      }
    }
    setConfirming(null)
    setPublishPhase(null)
    publishInFlightRef.current = false
  }

  const latestUnsortedTime = overview.unsortedEntries.reduce((latest, entry) => (
    typeof entry?.ts === 'number' && entry.ts > latest ? entry.ts : latest
  ), 0)

  function renderContributionRecord(record) {
    const attention = contributionNeedsAttention(record)
    const recordStage = contributionStage(record)
    const canOpenPr = typeof record?.url === 'string' && record.url.startsWith('https://github.com/')
    const action = publicationAction(record)
    const publicationPending = record?.status === 'submitting'
      && record?.successor !== true
    const attentionMessage = attention
      ? String(record?.attention?.message
        || record?.last_submit_error
        || record?.review?.message
        || '').trim()
      : ''
    const number = Number(record?.number)
    const meta = [record?.repo, Number.isInteger(number) && number > 0 ? `PR #${number}` : '', updateTime(record?.updated_at)].filter(Boolean).join(' · ')
    return (
      <article className={`chat-work__contribution is-${recordStage || activeStage}${attention ? ' needs-attention' : ''}`} key={recordRevision(record)}>
        <div className="chat-work__contribution-copy">
          <span className="chat-work__contribution-state">{lifecycleStatus(record, recordStage)}</span>
          <strong>{record?.summary || record?.title || (record?.kind === 'local' ? localDispositionLabel(record) : 'Contribution from this chat')}</strong>
          {meta ? <small>{meta}</small> : null}
          {attentionMessage ? <small className="is-error">{attentionMessage}</small> : null}
          {record?.kind === 'local' ? <code>{record.path}</code> : null}
          {failures[record.id] ? <small className="is-error">{failures[record.id].message}</small> : null}
        </div>
        <div className="chat-work__contribution-actions">
          {publicationPending ? (
            <>
              <button type="button" className="is-primary" disabled>{action.busyLabel}…</button>
              <button type="button" onClick={() => openContribute(record)}>Details</button>
            </>
          ) : attention && onContinueInChat ? (
            <button
              type="button"
              className="is-primary"
              disabled={helperStarting || workActive}
              title={workActive
                ? 'A helper is already preparing this batch — it will repair this review.'
                : undefined}
              onClick={() => continueInChat(record)}
            >
              {helperStarting ? 'Starting helper…' : 'Ask agent to fix'}
            </button>
          ) : recordStage === 'open' ? (
            <>
              {canOpenPr ? <a href={record.url} target="_blank" rel="noopener noreferrer">Open PR</a> : null}
              {onCheckUpdates && !workActive ? (
                <button
                  type="button"
                  disabled={helperStarting}
                  onClick={() => requestHelper(
                    () => onCheckUpdates([record], workContext),
                  )}
                >
                  {helperStarting ? 'Starting…' : 'Check for update'}
                </button>
              ) : null}
            </>
          ) : record?.kind === 'local' ? null : canOpenPr ? (
            <a href={record.url} target="_blank" rel="noopener noreferrer">Open on GitHub</a>
          ) : (
            <button type="button" onClick={() => openContribute(record)}>Details</button>
          )}
        </div>
      </article>
    )
  }

  return (
    <div className="chat-work__overlay" role="presentation">
      <div
        ref={dialogRef}
        className="chat-work chat-work--lifecycle"
        role="dialog"
        aria-labelledby="chat-work-diff-title"
      >
        <header className="chat-work__head">
          <div>
            <h2 id="chat-work-diff-title">Changes from this chat</h2>
          </div>
          <button ref={closeRef} type="button" className="chat-work__close" onClick={onClose} aria-label="Close changes">
            <X width={19} height={19} />
          </button>
        </header>

        <div className="chat-work__review-bar">
          <div className="chat-work__stages" role="tablist" aria-label="Contribution state">
          {SURFACE_STAGES.map(stage => (
            <button
              type="button"
              role="tab"
              key={stage}
              id={`chat-work-tab-${stage}`}
              aria-controls={`chat-work-panel-${stage}`}
              className={activeStage === stage ? 'is-active' : ''}
              aria-selected={activeStage === stage}
              tabIndex={activeStage === stage ? 0 : -1}
              onClick={() => setActiveStage(stage)}
              onKeyDown={event => selectStageFromKeyboard(event, stage)}
            >
              <span>
                {stage === 'working' && !overview.lifecycleAvailable
                  ? 'Recorded'
                  : STAGE_LABELS[stage]}
              </span>
              <b>{surfaceCounts[stage] || 0}</b>
            </button>
          ))}
          </div>
          <div className="chat-work__stage-actions">
          {activeStage === 'working' && unsortedGroups.length > 0 ? (
            <button
              type="button"
              onClick={() => setEveryDiffExpanded(expansionCommand?.expanded !== true)}
            >
              {expansionCommand?.expanded === true ? 'Collapse all' : 'Expand all'}
            </button>
          ) : null}
          </div>
        </div>

        <div
          id={`chat-work-panel-${activeStage}`}
          className="chat-work__body"
          role="tabpanel"
          aria-labelledby={`chat-work-tab-${activeStage}`}
        >
          {overview.loading && !overview.hasWork ? (
            <p className="chat-work__state" role="status">Loading changes…</p>
          ) : overview.error && !overview.hasWork ? (
            <p className="chat-work__state chat-work__state--error" role="alert">Could not refresh this chat’s complete change history.</p>
          ) : activeStage === 'working' ? (
            unsortedGroups.length > 0 || workingRecords.length > 0 ? (
              <div className="chat-work__updates">
                {unsortedGroups.length > 0 && !overview.lifecycleAvailable ? (
                  <p className="chat-work__notice">
                    Contribution status is unavailable. These are recorded edits, not a confirmed list of unorganized work.
                  </p>
                ) : unsortedGroups.length > 0 && overview.error ? (
                  <p className="chat-work__notice">Showing the changes already loaded in this chat.</p>
                ) : null}
                {shortenedCount > 0 ? <p className="chat-work__notice">{shortenedCount} older {shortenedCount === 1 ? 'update is' : 'updates are'} excerpt-only.</p> : null}
                {unsortedGroups.map((group) => (
                  <section className="chat-work__update" key={group.id}>
                    <div className="chat-work__update-head">
                      <div>
                        <span className="chat-work__update-number">{group.label}</span>
                        <strong>{group.files.length} {group.files.length === 1 ? 'file' : 'files'}</strong>
                      </div>
                      <div className="chat-work__update-head-actions">
                        {latestUnsortedTime ? <span>{updateTime(latestUnsortedTime)}</span> : null}
                      </div>
                    </div>
                    <FileDiffList
                      files={group.files}
                      diffTruncated={shortenedCount > 0}
                      expansionCommand={expansionCommand}
                    />
                  </section>
                ))}
                {workingRecords.length > 0 ? (
                  <div className="chat-work__contributions">
                    {workingRecords.map(renderContributionRecord)}
                  </div>
                ) : null}
              </div>
            ) : <EmptyStage stage="working" hasRecordedEdits={overview.counts.files > 0} />
          ) : activeStage === 'ready' && visiblePreparedItems.length > 0 ? (
            <div className={`chat-work__review-workspace${visiblePreparedItems.length === 1 ? ' is-single' : ''}`}>
              {visiblePreparedItems.length > 1 ? <nav className="chat-work__review-list" aria-label="Ready contributions">
                {visiblePreparedItems.map(item => {
                  const representative = preparedRepresentative(item, visiblePrepared)
                  const key = preparedItemRevision(item)
                  return (
                    <button
                      type="button"
                      key={key}
                      className={key === preparedItemRevision(selectedPreparedItem) ? 'is-active' : ''}
                      aria-pressed={key === preparedItemRevision(selectedPreparedItem)}
                      onClick={() => setSelectedPreparedKey(key)}
                    >
                      <strong>{preparedTitle(item, representative)}</strong>
                      <small>{[
                        representative?.repo,
                        item.kind === 'stack' ? `${item.records.length} linked changes` : '',
                      ].filter(Boolean).join(' · ')}</small>
                    </button>
                  )
                })}
              </nav> : null}
              {selectedPreparedItem ? (() => {
                const stack = selectedPreparedItem.kind === 'stack'
                const representative = preparedRepresentative(selectedPreparedItem, visiblePrepared)
                const blocker = stack
                  ? stackSendBlocker(selectedPreparedItem, { connected: overview.contributions?.connected !== false })
                  : sendBlocker(representative, { connected: overview.contributions?.connected !== false })
                const action = stack
                  ? publicationStackAction(selectedPreparedItem)
                  : publicationAction(representative)
                const pending = stack
                  ? selectedPreparedItem.records.some(record => record?.status === 'submitting')
                  : representative?.status === 'submitting'
                    && representative?.successor !== true
                const records = stack ? selectedPreparedItem.records : [representative]
                return (
                  <article className="chat-work__review-detail">
                    <header>
                      <div>
                        <span>{stack ? 'Ready stack' : 'Ready'}</span>
                        <h3>{preparedTitle(selectedPreparedItem, representative)}</h3>
                        <small>{[
                          representative?.repo,
                          updateTime(representative?.updated_at),
                        ].filter(Boolean).join(' · ')}</small>
                      </div>
                      <div className="chat-work__contribution-actions">
                        {pending ? (
                          <button type="button" className="is-primary" disabled>Confirming…</button>
                        ) : !blocker && visiblePreparedItems.length > 1 ? (
                          <button type="button" onClick={() => beginConfirmation([selectedPreparedItem])}>{action.label}</button>
                        ) : blocker && visiblePreparedItems.length > 1 && onContributeAll ? (
                          <button
                            type="button"
                            disabled={helperStarting || workActive}
                            title={workActive
                              ? 'A helper is already preparing this batch — it will repair this review.'
                              : undefined}
                            onClick={() => requestHelper(
                              () => onContributeAll(overview.workflowRevision, workContext),
                            )}
                          >
                            {helperStarting ? 'Starting helper…' : 'Fix and review'}
                          </button>
                        ) : null}
                        <button type="button" onClick={() => openContribute(representative)}>Open workshop</button>
                      </div>
                    </header>
                    {blocker ? <p className="chat-work__review-blocker">{blocker}</p> : null}
                    <div className="chat-work__review-diffs">
                      {records.filter(Boolean).map((record, index) => (
                        <section key={record.id}>
                          {records.length > 1 ? <h4>{index + 1}. {record.summary || record.title || record.repo}</h4> : null}
                          <ChatContributionDiff appId={overview.contributeAppId} record={record} />
                        </section>
                      ))}
                    </div>
                  </article>
                )
              })() : null}
            </div>
          ) : activeStage === 'needs_you' && (workDecision || attentionRecords.length > 0) ? (
            <div className="chat-work__contributions">
              {workDecision ? (
                <article className="chat-work__contribution needs-attention chat-work__work-decision" key="work-decision">
                  <div className="chat-work__contribution-copy">
                    <span className="chat-work__contribution-state">Needs your decision</span>
                    <strong>Preparation finished without changes to send</strong>
                    <small className="is-error">The last prepare made no private review and opened no pull request.</small>
                    <details className="chat-work__work-decision-detail">
                      <summary>Why this needs you</summary>
                      <div>{workDecision.result}</div>
                    </details>
                  </div>
                </article>
              ) : null}
              {attentionRecords.map(renderContributionRecord)}
            </div>
          ) : visibleRecords.length > 0 ? (
            <div className="chat-work__contributions">
              {visibleRecords.map(renderContributionRecord)}
            </div>
          ) : <EmptyStage stage={activeStage} hasRecordedEdits={overview.counts.files > 0} />}
        </div>

        <footer className="chat-work__dock" aria-label="Contribution controls">
          <div className="chat-work__dock-main">
            {helperStopError ? (
              <section className="chat-work__primary-actions is-attention" role="alert">
                <div>
                  <strong>Preparation is still running</strong>
                  <span>{helperStopError}</span>
                </div>
                <div className="chat-work__primary-buttons">
                  <button type="button" onClick={reviewContributionOutcome}>Review</button>
                  <button type="button" disabled={helperStopping} onClick={() => stopHelper(work)}>
                    {helperStopping ? 'Stopping…' : 'Try stop again'}
                  </button>
                </div>
              </section>
            ) : helperStartError ? (
              <section
                className="chat-work__primary-actions is-attention"
                role="alert"
              >
                <div>
                  <strong>Preparation paused</strong>
                  <span>{helperStartError}</span>
                </div>
                <div className="chat-work__primary-buttons">
                  <button type="button" onClick={reviewContributionOutcome}>Review</button>
                  {retryHelperRef.current ? (
                    <button type="button" className="is-primary" disabled={helperStarting} onClick={retryHelper}>
                      {helperStarting ? 'Starting…' : 'Try again'}
                    </button>
                  ) : null}
                </div>
              </section>
            ) : workState === 'active' || workState === 'attention' ? (
              <section className={`chat-work__primary-actions is-${workState}`} aria-live="polite">
                <div>
                  <strong>{preparationView?.title}</strong>
                  <span>{preparationView?.copy}</span>
                </div>
                <div className="chat-work__primary-buttons">
                  <button type="button" onClick={reviewContributionOutcome}>Review</button>
                  {workState === 'attention' ? (
                    <button
                      type="button"
                      className="is-primary"
                      disabled={helperStarting}
                      onClick={() => requestHelper(
                        () => onContributeAll?.(overview.workflowRevision, workContext),
                      )}
                    >
                      {helperStarting ? 'Starting…' : 'Try again'}
                    </button>
                  ) : (
                    <button type="button" className="is-primary" disabled>Preparing…</button>
                  )}
                  {workState === 'active' && typeof onStopWork === 'function' ? (
                    <button type="button" disabled={helperStopping} onClick={() => stopHelper(work)}>
                      {helperStopping ? 'Stopping…' : 'Stop'}
                    </button>
                  ) : null}
                </div>
              </section>
            ) : activeStage === 'done' ? null : outcomeActions ? (
              <section className={`chat-work__primary-actions${workDecision ? ' is-attention' : ''}`} aria-label="Contribution outcome">
                <div>
                  <strong>{workDecision ? 'Preparation finished — needs your decision' : 'Choose an outcome'}</strong>
                  <span>{workDecision
                    ? 'The last prepare made no private review and opened no pull request. Here is why, before you retry.'
                    : 'Review the work, prepare it privately, or keep the guarded merge cycle moving.'}</span>
                  {workDecision ? (
                    <details className="chat-work__work-decision-detail">
                      <summary>Why this needs you</summary>
                      <div>{workDecision.result}</div>
                    </details>
                  ) : null}
                </div>
                <div className="chat-work__primary-buttons">
                  <button
                    type="button"
                    title={outcomeActions.review.description}
                    onClick={reviewContributionOutcome}
                  >
                    {outcomeActions.review.label}
                  </button>
                  <button
                    type="button"
                    title={outcomeActions.prepare.description}
                    disabled={helperStarting || outcomeActions.prepare.disabled}
                    onClick={prepareContributionOutcome}
                  >
                    {helperStarting ? 'Starting…' : outcomeActions.prepare.label}
                  </button>
                  <button
                    type="button"
                    className="is-primary"
                    title={outcomeActions.merge.description}
                    disabled={helperStarting}
                    onClick={mergeContributionOutcome}
                  >
                    {helperStarting ? 'Starting…' : outcomeActions.merge.label}
                  </button>
                </div>
              </section>
            ) : null}
          </div>

        </footer>

        {confirming ? (
          <div className="chat-work__confirm" role="alertdialog" aria-label="Confirm public contribution actions">
            <div>
              <strong>{confirmingAction.promptLabel}</strong>
              <span className={confirmationNotice ? 'is-attention' : ''}>
                {confirmationNotice || 'This step opens or updates only these exact reviewed pull requests. Nothing merges yet.'}
              </span>
              {confirmingMutations.length > 1 ? (
                <ul className="chat-work__confirm-mutations">
                  {confirmingMutations.map(mutation => (
                    <li key={mutation}>{mutation}</li>
                  ))}
                </ul>
              ) : null}
            </div>
            <div>
              <button type="button" disabled={helperStarting || Boolean(publishPhase)} onClick={() => {
                setConfirming(null)
                setConfirmationNotice('')
              }}>Keep private</button>
              <button type="button" className="is-primary" disabled={helperStarting || Boolean(publishPhase)} onClick={() => publishBatch(confirming)}>
                {publishPhase === 'checking'
                  ? 'Checking…'
                  : publishPhase === 'publishing'
                    ? confirmingAction.updating ? 'Updating…' : 'Sending…'
                    : publishPhase === 'recovering'
                      ? 'Starting helper…'
                    : confirmingAction.confirmLabel}
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  )
}
