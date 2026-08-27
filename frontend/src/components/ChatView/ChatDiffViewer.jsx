/* Chat-scoped Changes workspace: unsorted edits, prepared work, PRs, and history. */

import { useEffect, useRef, useState } from 'react'
import { X } from '@openai/apps-sdk-ui/components/Icon'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { formatRelativeTime } from '../../lib/relativeTime.js'
import FileDiffList from '../DiffView/FileDiffList.jsx'
import { chatContributionPrepareAction } from './chatContributionIntent.js'
import {
  CHANGE_STAGES,
  compactChangesSummary,
  contributionNeedsAttention,
  initialChangesStage,
} from './chatChangesLifecycle.js'
import { contributionReviewIntent } from './contributionReviewModel.js'
import { useChatChangesOverview } from './useChatChangesOverview.js'
import './ChatWork.css'

const STAGE_LABELS = {
  unsorted: 'Unsorted',
  prepared: 'Prepared',
  open: 'Open',
  landed: 'Landed',
}

function updateTime(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return formatRelativeTime(new Date(value).toISOString())
  }
  return formatRelativeTime(value)
}

function lifecycleStatus(record, stage) {
  if (contributionNeedsAttention(record)) return 'Needs attention'
  if (record?.status === 'submitting') return 'Publishing'
  if (record?.status === 'draft') return 'Draft PR'
  if (record?.status === 'landing') return 'Merging'
  if (record?.status === 'superseded') return 'Already shared'
  if (record?.status === 'closed') return 'Not merged'
  if (stage === 'prepared') return 'Private review'
  if (stage === 'open') return 'PR open'
  return 'Merged'
}

function LifecycleRow({
  record, stage, turnActive, onOpenContribute, onContinueInChat,
}) {
  const attention = contributionNeedsAttention(record)
  const number = Number(record?.number)
  const meta = [
    record?.repo,
    Number.isInteger(number) && number > 0 ? `PR #${number}` : '',
  ].filter(Boolean).join(' · ')
  const title = record?.summary || record?.title || 'Contribution from this chat'
  const canOpenPr = typeof record?.url === 'string'
    && record.url.startsWith('https://github.com/')

  return (
    <article className={`chat-work__contribution is-${stage}${attention ? ' needs-attention' : ''}`}>
      <div className="chat-work__contribution-copy">
        <span className="chat-work__contribution-state">
          {lifecycleStatus(record, stage)}
        </span>
        <strong>{title}</strong>
        {meta ? <small>{meta}</small> : null}
      </div>
      <div className="chat-work__contribution-actions">
        {attention && typeof onContinueInChat === 'function' ? (
          <button type="button" onClick={() => onContinueInChat(record)}>
            {turnActive ? 'Queue agent follow-up' : 'Ask agent to fix'}
          </button>
        ) : stage === 'prepared' ? (
          <button type="button" onClick={() => onOpenContribute(record)}>
            Review &amp; send
          </button>
        ) : canOpenPr ? (
          <a href={record.url} target="_blank" rel="noopener noreferrer">
            Open PR
          </a>
        ) : (
          <button type="button" onClick={() => onOpenContribute(record)}>
            Details
          </button>
        )}
      </div>
    </article>
  )
}

function EmptyStage({ stage, hasRecordedEdits }) {
  const copy = {
    unsorted: hasRecordedEdits
      ? ['Everything is organized', 'Every recorded file is already covered by prepared or published work.']
      : ['No file changes yet', 'Edits made through this chat will collect here automatically.'],
    prepared: ['Nothing prepared', 'Private reviews created from this chat will appear here.'],
    open: ['No open pull requests', 'Published work stays here while it moves through review and checks.'],
    landed: ['Nothing landed yet', 'Merged and otherwise settled work will collect here.'],
  }[stage]
  return (
    <div className="chat-work__empty">
      <strong>{copy[0]}</strong>
      <span>{copy[1]}</span>
    </div>
  )
}

export default function ChatDiffViewer({
  chatId,
  initialEntries,
  onClose,
  onPrepareChanges,
  onOpenApp,
  onContinueInChat,
  turnActive = false,
}) {
  const overview = useChatChangesOverview(chatId, initialEntries)
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const expansionSequenceRef = useRef(0)
  const stageSeededRef = useRef(false)
  const [activeStage, setActiveStage] = useState('unsorted')
  const [expansionCommand, setExpansionCommand] = useState(null)

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: closeRef,
    onClose,
  })

  useEffect(() => {
    if (overview.loading || stageSeededRef.current) return
    stageSeededRef.current = true
    setActiveStage(initialChangesStage(overview))
  }, [overview])

  function setEveryDiffExpanded(expanded) {
    expansionSequenceRef.current += 1
    setExpansionCommand({ id: expansionSequenceRef.current, expanded })
  }

  function openContribute(record) {
    const intent = contributionReviewIntent(record)
    if (!overview.contributeApp || !onOpenApp || !intent) return
    onOpenApp(overview.contributeApp, { final: true, intent })
    onClose?.()
  }

  function continueInChat(record) {
    onContinueInChat?.(record)
    onClose?.()
  }

  const summary = compactChangesSummary(overview)
  const visibleRecords = overview.stages[activeStage] || []
  const shortenedCount = overview.unsortedEntries.filter(
    entry => entry.preview?.truncated,
  ).length
  const latestUnsortedTime = overview.unsortedEntries.reduce((latest, entry) => (
    typeof entry?.ts === 'number' && entry.ts > latest ? entry.ts : latest
  ), 0)
  const prepareAction = chatContributionPrepareAction(turnActive)

  return (
    <div className="chat-work__overlay" role="presentation" onClick={onClose}>
      <div
        ref={dialogRef}
        className="chat-work chat-work--diffs chat-work--lifecycle"
        role="dialog"
        aria-modal="true"
        aria-labelledby="chat-work-diff-title"
        onClick={event => event.stopPropagation()}
      >
        <header className="chat-work__head">
          <div>
            <h2 id="chat-work-diff-title">Changes from this chat</h2>
            <p>{summary}</p>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="chat-work__close"
            onClick={onClose}
            aria-label="Close changes"
          >
            <X width={19} height={19} />
          </button>
        </header>

        <nav className="chat-work__stages" aria-label="Change stages">
          {CHANGE_STAGES.map(stage => (
            <button
              type="button"
              key={stage}
              className={activeStage === stage ? 'is-active' : ''}
              aria-pressed={activeStage === stage}
              onClick={() => setActiveStage(stage)}
            >
              <span>{STAGE_LABELS[stage]}</span>
              <b>{overview.counts[stage] || 0}</b>
            </button>
          ))}
        </nav>

        {activeStage === 'unsorted' && overview.unsortedEntries.length > 0 ? (
          <div className="chat-work__toolbar">
            <div role="group" aria-label="Diff display controls">
              <button type="button" onClick={() => setEveryDiffExpanded(true)}>Expand all</button>
              <button type="button" onClick={() => setEveryDiffExpanded(false)}>Collapse all</button>
            </div>
          </div>
        ) : null}

        <div className="chat-work__body">
          {overview.loading && !overview.hasWork ? (
            <p className="chat-work__state" role="status">Loading changes…</p>
          ) : overview.error && !overview.hasWork ? (
            <p className="chat-work__state chat-work__state--error" role="alert">
              Could not refresh this chat’s complete change history.
            </p>
          ) : activeStage === 'unsorted' ? (
            overview.unsortedEntries.length > 0 ? (
              <div className="chat-work__updates">
                {overview.error ? (
                  <p className="chat-work__notice">Showing the changes already loaded in this chat.</p>
                ) : null}
                {shortenedCount > 0 ? (
                  <p className="chat-work__notice">
                    {shortenedCount === 1
                      ? '1 older update is excerpt-only because its complete diff was never saved.'
                      : `${shortenedCount} older updates are excerpt-only because their complete diffs were never saved.`}
                  </p>
                ) : null}
                <section className="chat-work__update">
                  <div className="chat-work__update-head">
                    <div>
                      <span className="chat-work__update-number">Unsorted work</span>
                      <strong>
                        {overview.counts.unsorted === 1
                          ? '1 file'
                          : `${overview.counts.unsorted} files`}
                        {overview.unsortedEntries.length > 1
                          ? ` · ${overview.unsortedEntries.length} updates`
                          : ''}
                      </strong>
                    </div>
                    {latestUnsortedTime ? <span>{updateTime(latestUnsortedTime)}</span> : null}
                  </div>
                  <FileDiffList
                    files={overview.unsortedFiles}
                    diffTruncated={shortenedCount > 0}
                    expansionCommand={expansionCommand}
                  />
                  {overview.unsortedEntries.some(entry => entry.preview?.relative) ? (
                    <p className="chat-work__update-note">Some line numbers are relative to the edited selection.</p>
                  ) : null}
                </section>
              </div>
            ) : <EmptyStage stage="unsorted" hasRecordedEdits={overview.counts.files > 0} />
          ) : visibleRecords.length > 0 ? (
            <div className="chat-work__contributions">
              {visibleRecords.map(record => (
                <LifecycleRow
                  key={record.id}
                  record={record}
                  stage={activeStage}
                  turnActive={turnActive}
                  onOpenContribute={openContribute}
                  onContinueInChat={continueInChat}
                />
              ))}
            </div>
          ) : <EmptyStage stage={activeStage} hasRecordedEdits={overview.counts.files > 0} />}
        </div>

        {activeStage === 'unsorted' && overview.counts.unsorted > 0 && onPrepareChanges ? (
          <footer className="chat-work__prepare">
            <div>
              <strong>Organize this work</strong>
              <span id="chat-work-prepare-description">{prepareAction.description}</span>
            </div>
            <button
              type="button"
              onClick={onPrepareChanges}
              aria-describedby="chat-work-prepare-description"
            >
              {prepareAction.label}
            </button>
          </footer>
        ) : null}
      </div>
    </div>
  )
}
