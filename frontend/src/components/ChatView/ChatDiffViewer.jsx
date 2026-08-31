/* Complete chat-scoped contribution control surface. */

import { useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { X } from '@openai/apps-sdk-ui/components/Icon'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { formatRelativeTime } from '../../lib/relativeTime.js'
import FileDiffList from '../DiffView/FileDiffList.jsx'
import ChatContributionDiff from './ChatContributionDiff.jsx'
import {
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
  reviewItems,
  refreshedReviewItems,
  sendBlocker,
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

function AttachedWorkPanel({
  work, state, onStop, onContinue, onOpenChat, action = null, onAction,
  busy = false, stopping = false,
}) {
  const waitingForSource = state === 'active' && work?.status === 'accepted'
  const retryingStart = state === 'active' && work?.status === 'retrying'
  const view = {
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
  if (!view) return null
  const usageLabel = tokenUsageLabel(work)
  return (
    <section className={`chat-work__helper is-${state}`} aria-live="polite">
      <div className="chat-work__helper-copy">
        <strong>{view.title}</strong>
        <span>{view.copy}</span>
        {usageLabel ? <small>{usageLabel}</small> : null}
      </div>
      <div className="chat-work__helper-actions">
        {action && typeof onAction === 'function' ? (
          <button type="button" className="is-primary" disabled={busy} onClick={onAction}>
            {busy ? 'Starting…' : action.label}
          </button>
        ) : null}
        {state === 'attention' && typeof onContinue === 'function' ? (
          <button type="button" disabled={busy} onClick={onContinue}>
            {busy ? 'Starting…' : 'Continue preparation'}
          </button>
        ) : null}
        {work?.child_chat_id && typeof onOpenChat === 'function' ? (
          <button type="button" className="is-secondary" onClick={() => onOpenChat(work.child_chat_id)}>
            View helper
          </button>
        ) : null}
        {state === 'active' && typeof onStop === 'function' ? (
          <button type="button" className="is-secondary" disabled={busy || stopping} onClick={() => onStop(work)}>
            {stopping ? 'Stopping…' : 'Stop'}
          </button>
        ) : null}
      </div>
    </section>
  )
}

const WORK_INTENT_LABELS = {
  prepare: 'Prepare changes',
  finish: 'Prepare all changes',
  project: 'Prepare project',
  updates: 'Check updates',
  followup: 'Review follow-up',
}

const WORK_STATUS_LABELS = {
  accepted: 'Waiting',
  queued: 'Waiting',
  retrying: 'Retrying',
  starting: 'Starting',
  running: 'Working',
  resuming: 'Resuming',
  paused: 'Paused',
  completed: 'Completed',
  cancelled: 'Stopped',
  stopped: 'Stopped',
  failed: 'Needs attention',
  interrupted: 'Needs attention',
  needs_review: 'Needs attention',
}

function tokenUsageLabel(work) {
  const rawTotal = work?.usage?.totals?.total_tokens
  if (rawTotal === null || rawTotal === undefined || rawTotal === '') return ''
  const totalTokens = Number(rawTotal)
  if (!Number.isFinite(totalTokens) || totalTokens < 0) return ''
  return `${new Intl.NumberFormat(undefined, {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(totalTokens)} tokens`
}

function PreparationHistory({
  count = 0,
  excludeWorkId = '',
  onLoad,
  onOpenChat,
}) {
  const visibleCount = Math.max(0, Number(count || 0) - (excludeWorkId ? 1 : 0))
  const [phase, setPhase] = useState('idle')
  const [items, setItems] = useState([])
  const [error, setError] = useState('')
  const [truncation, setTruncation] = useState(null)

  if (visibleCount === 0 || typeof onLoad !== 'function') return null

  const fetchHistory = async () => {
    setPhase('loading')
    setError('')
    const outcome = await onLoad()
    if (outcome?.kind === 'loaded') {
      setItems((outcome.items || []).filter(item => item?.id !== excludeWorkId))
      setTruncation(outcome.truncated === true ? {
        shown: (outcome.items || []).length,
        total: Number(outcome.total) || visibleCount,
      } : null)
      setPhase('ready')
      return
    }
    setError(String(outcome?.message || '').trim()
      || 'Preparation history could not be loaded. Close this section and try again.')
    setPhase('error')
  }

  const loadHistory = event => {
    if (!event.currentTarget.open || phase !== 'idle') return
    void fetchHistory()
  }

  return (
    <details className="chat-work__history" onToggle={loadHistory}>
      <summary>
        <span>Preparation history</span>
        <small>{visibleCount}</small>
      </summary>
      <div className="chat-work__history-body">
        {phase === 'loading' ? (
          <div className="chat-work__history-state" role="status">Loading helpers…</div>
        ) : null}
        {phase === 'error' ? (
          <div className="chat-work__history-state is-error" role="alert">
            <span>{error}</span>
            <button type="button" onClick={fetchHistory}>Try again</button>
          </div>
        ) : null}
        {phase === 'ready' && items.length === 0 ? (
          <div className="chat-work__history-state">No earlier helpers.</div>
        ) : null}
        {phase === 'ready' ? items.map(item => {
          const status = WORK_STATUS_LABELS[item?.status] || 'Finished'
          const usage = tokenUsageLabel(item)
          return (
            <div className="chat-work__history-row" key={item.id}>
              <div className="chat-work__history-copy">
                <strong>{WORK_INTENT_LABELS[item?.intent] || 'Contribution preparation'}</strong>
                <small>
                  <span className={`is-${String(item?.status || 'finished')}`}>{status}</span>
                  {item?.created_at ? <span>{updateTime(item.created_at)}</span> : null}
                  {usage ? <span>{usage}</span> : null}
                </small>
              </div>
              {item?.child_chat_id && typeof onOpenChat === 'function' ? (
                <button type="button" onClick={() => onOpenChat(item.child_chat_id)}>
                  View
                </button>
              ) : null}
            </div>
          )
        }) : null}
        {phase === 'ready' && truncation ? (
          <div className="chat-work__history-state">
            Showing the newest {truncation.shown} of {truncation.total} helpers.
          </div>
        ) : null}
      </div>
    </details>
  )
}

export default function ChatDiffViewer({
  chatId,
  initialEntries,
  onClose,
  onPrepareChanges,
  onPrepareProject,
  onContributeAll,
  onCheckUpdates,
  onOpenApp,
  onOpenChat,
  onContinueInChat,
  onStopWork,
  onLoadWorkHistory,
  returnFocusRef,
}) {
  const queryClient = useQueryClient()
  const overview = useChatChangesOverview(chatId, initialEntries)
  const work = overview.work || null
  const workState = contributionWorkState(work)
  const workContext = contributionWorkContext(overview)
  const workActive = workState === 'active'
  const publicationPending = overview.counts.submitting > 0
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
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

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: closeRef,
    restoreFocusRef: returnFocusRef,
    onClose,
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
  const surfaceCounts = {
    working: overview.counts.unsorted + workingRecords.length,
    ready: readyRecords.length,
    needs_you: attentionRecords.length,
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
  const primaryAction = activeStage === 'ready'
    ? preparedPrimaryAction
    : lifecycleAction?.kind === 'review'
      ? preparedPrimaryAction
      : lifecycleAction
  const confirmingAction = confirming ? publicationItemsAction(confirming) : null

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

  async function runPrimaryAction() {
    if (!primaryAction) return
    if (primaryAction.kind === 'prepare') {
      await requestHelper(
        () => onPrepareChanges?.(overview.unsortedRevision, workContext),
      )
      return
    }
    if (primaryAction.kind === 'finish') {
      await requestHelper(
        () => onContributeAll?.(overview.workflowRevision, workContext),
      )
      return
    }
    if (primaryAction.kind === 'publish-items') {
      beginConfirmation(primaryAction.items)
      return
    }
    if (primaryAction.kind === 'fix-prepared') {
      await requestHelper(
        () => onContributeAll?.(overview.workflowRevision, workContext),
      )
      return
    }
    if (primaryAction.kind === 'updates') {
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
    if (item?.kind === 'stack') item.records.forEach(consume)
    else if (item?.record) consume(item.record)
  }

  function releaseItem(item, failure) {
    if (item?.kind === 'stack') item.records.forEach(record => release(record, failure))
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
              disabled={helperStarting}
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
    <div className="chat-work__overlay" role="presentation" onClick={onClose}>
      <div
        ref={dialogRef}
        className="chat-work chat-work--lifecycle"
        role="dialog"
        aria-modal="true"
        aria-labelledby="chat-work-diff-title"
        onClick={event => event.stopPropagation()}
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
                {unsortedGroups.map((group, groupIndex) => (
                  <section className="chat-work__update" key={group.id}>
                    <div className="chat-work__update-head">
                      <div>
                        <span className="chat-work__update-number">{group.label}</span>
                        <strong>{group.files.length} {group.files.length === 1 ? 'file' : 'files'}</strong>
                      </div>
                      <div className="chat-work__update-head-actions">
                        {latestUnsortedTime ? <span>{updateTime(latestUnsortedTime)}</span> : null}
                        {unsortedGroups.length > 1
                          && overview.lifecycleAvailable && onPrepareProject
                          && !workActive && !publicationPending ? (
                          <button
                            type="button"
                            disabled={helperStarting}
                            onClick={() => requestHelper(
                              () => onPrepareProject(group, overview.unsortedRevision, workContext),
                            )}
                          >
                            {helperStarting ? 'Starting…' : 'Prepare'}
                          </button>
                        ) : null}
                      </div>
                    </div>
                    <FileDiffList
                      files={group.files}
                      diffTruncated={shortenedCount > 0}
                      expansionCommand={expansionCommand}
                      initiallyOpenFirst={groupIndex === 0}
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
                const pending = stack
                  ? selectedPreparedItem.records.some(record => record?.status === 'submitting')
                  : representative?.status === 'submitting'
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
          ) : visibleRecords.length > 0 ? (
            <div className="chat-work__contributions">
              {visibleRecords.map(renderContributionRecord)}
            </div>
          ) : <EmptyStage stage={activeStage} hasRecordedEdits={overview.counts.files > 0} />}
          {activeStage === 'working' ? (
            <div className="chat-work__history-slot">
              <PreparationHistory
                count={overview.workHistoryCount}
                excludeWorkId={workState === 'active' || workState === 'attention' ? work?.id : ''}
                onLoad={() => onLoadWorkHistory?.({ appId: overview.contributeAppId })}
                onOpenChat={onOpenChat}
              />
            </div>
          ) : null}
        </div>

        <footer className="chat-work__dock" aria-label="Contribution controls">
          <div className="chat-work__dock-main">
            {helperStopError ? (
              <section className="chat-work__helper-request is-error" role="alert">
                <strong>Preparation is still running</strong>
                <span>{helperStopError}</span>
              </section>
            ) : helperStarting || helperStartError ? (
              <section
                className={`chat-work__helper-request${helperStartError ? ' is-attention' : ' is-starting'}`}
                role="status"
                aria-live="polite"
              >
                <strong>
                  {helperStartError ? 'Preparation paused' : 'Starting preparation…'}
                </strong>
                <span>{helperStartError || 'Changes stays open while the background helper starts.'}</span>
                {helperStartError && retryHelperRef.current ? (
                  <button type="button" disabled={helperStarting} onClick={retryHelper}>
                    {helperStarting ? 'Starting…' : 'Try again'}
                  </button>
                ) : null}
              </section>
            ) : workState === 'active' || workState === 'attention' ? (
              <AttachedWorkPanel
                work={work}
                state={workState}
                onStop={stopHelper}
                onContinue={() => requestHelper(
                  () => onContributeAll?.(overview.workflowRevision, workContext),
                )}
                onOpenChat={onOpenChat}
                action={primaryAction?.kind === 'publish-items' ? primaryAction : null}
                onAction={runPrimaryAction}
                busy={helperStarting}
                stopping={helperStopping}
              />
            ) : activeStage === 'needs_you'
              || activeStage === 'done' ? null : (
              <section className="chat-work__primary-actions" aria-label="Next contribution action">
                <div>
                  <strong>{primaryAction ? 'Next step' : 'This chat is settled'}</strong>
                  <span>{primaryAction?.description || 'Public work and local decisions remain connected to this chat.'}</span>
                </div>
                {primaryAction ? (
                  <div className="chat-work__primary-buttons">
                    <button
                      type="button"
                      className="is-primary"
                      disabled={helperStarting}
                      onClick={runPrimaryAction}
                    >
                      {helperStarting ? 'Starting…' : primaryAction.label}
                    </button>
                  </div>
                ) : null}
              </section>
            )}
          </div>

        </footer>

        {confirming ? (
          <div className="chat-work__confirm" role="alertdialog" aria-label="Confirm public contribution actions">
            <div>
              <strong>{confirmingAction.promptLabel}</strong>
              <span className={confirmationNotice ? 'is-attention' : ''}>
                {confirmationNotice || 'GitHub will receive only these exact reviewed heads. Nothing will be merged.'}
              </span>
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
