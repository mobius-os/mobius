/** ChatUsageInspector shows honest chat totals and expandable per-turn usage. */

import { useRef } from 'react'
import {
  ArrowRotateCw,
  ChevronRight,
  InfoCircle,
  X,
} from '@openai/apps-sdk-ui/components/Icon'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { chatQueries } from '../../hooks/queries.js'
import {
  formatCacheHitRate,
  formatCostUsd,
  formatTimestamp,
  formatTokenCount,
  nonCachedInputTokens,
  usageModelName,
} from './chatUsageFormat.js'
import './ChatUsageInspector.css'

const RUN_COUNT_FIELDS = [
  'input_tokens',
  'output_tokens',
  'cache_read_input_tokens',
  'cache_creation_input_tokens',
  'reasoning_output_tokens',
  'total_tokens',
]

function statusLabel(status) {
  if (!status) return 'Unknown status'
  return status.replaceAll('_', ' ').replace(/^./, char => char.toUpperCase())
}

function hasRecordedUsage(run) {
  return Boolean(run.usage) || RUN_COUNT_FIELDS.some(
    field => typeof run[field] === 'number',
  )
}

function TotalsCell({ label, value }) {
  return (
    <div className="cui-totals__cell">
      <span className="cui-totals__label">{label}</span>
      <span className="cui-totals__value">{value ?? '—'}</span>
    </div>
  )
}

function RunField({ label, value }) {
  return (
    <div className="cui-run__field">
      <span className="cui-run__field-label">{label}</span>
      <span className="cui-run__field-value">{value ?? '—'}</span>
    </div>
  )
}

function RunSummary({ run, expandable }) {
  const when = formatTimestamp(run.started_at) || 'Unknown time'
  const cost = formatCostUsd(run.cost_usd)
  const outcome = cost
    ? cost
    : (run.status === 'completed' ? 'Cost unavailable' : statusLabel(run.status))
  const content = (
    <>
      <span className="cui-run__when">{when}</span>
      <span className="cui-run__meta">
        {run.provider && <span className="cui-run__badge">{run.provider}</span>}
        <span className="cui-run__outcome">{outcome}</span>
        {expandable && (
          <ChevronRight
            className="cui-run__chevron"
            width={16}
            height={16}
            aria-hidden="true"
          />
        )}
      </span>
    </>
  )
  return expandable
    ? <summary className="cui-run__summary">{content}</summary>
    : <div className="cui-run__summary cui-run__summary--static">{content}</div>
}

function RunRow({ run }) {
  const expandable = hasRecordedUsage(run)
  if (!expandable) {
    return (
      <div className="cui-run">
        <RunSummary run={run} expandable={false} />
      </div>
    )
  }

  return (
    <details className="cui-run">
      <RunSummary run={run} expandable />
      <div className="cui-run__detail">
        <RunField label="Input" value={formatTokenCount(nonCachedInputTokens(run))} />
        <RunField label="Cumulative input" value={formatTokenCount(run.input_tokens)} />
        <RunField label="Cached input" value={formatTokenCount(run.cache_read_input_tokens)} />
        <RunField label="Cache hit" value={formatCacheHitRate(run, 1)} />
        <RunField label="Cache write" value={formatTokenCount(run.cache_creation_input_tokens)} />
        <RunField label="Output" value={formatTokenCount(run.output_tokens)} />
        <RunField label="Reasoning within output" value={formatTokenCount(run.reasoning_output_tokens)} />
        <RunField label="Turn total" value={formatTokenCount(run.total_tokens)} />
        <RunField label="Model" value={usageModelName(run.usage)} />
        <RunField label="Context limit / call" value={formatTokenCount(run.model_context_window)} />
        <RunField label="Status" value={statusLabel(run.status)} />
      </div>
    </details>
  )
}

function UsageData({ data }) {
  const totals = data.totals || {}
  const runs = Array.isArray(data.runs) ? [...data.runs].reverse() : []
  const coverage = data.coverage

  if (runs.length === 0) {
    return <p className="cui__state">Usage appears after the first completed response.</p>
  }

  const missingUsage = coverage && coverage.runs_with_usage < coverage.runs
  return (
    <>
      <div className="cui-totals">
        <TotalsCell label="Input" value={formatTokenCount(nonCachedInputTokens(totals))} />
        <TotalsCell label="Output" value={formatTokenCount(totals.output_tokens)} />
        <TotalsCell label="Cache hit" value={formatCacheHitRate(totals, 1)} />
        <TotalsCell label="Cost" value={formatCostUsd(totals.cost_usd)} />
      </div>
      <div className="cui-cumulative">
        <span className="cui-cumulative__label">Cumulative input</span>
        <span className="cui-cumulative__value">
          {formatTokenCount(totals.input_tokens) ?? '—'}
        </span>
        <span className="cui-cumulative__hint">
          Across every model call, including cached context
        </span>
      </div>
      <p className="cui__note">
        <InfoCircle width={14} height={14} aria-hidden="true" />
        Input excludes cached tokens. Cost is estimated when the provider does
        not report it. Reasoning is included in output.
      </p>
      {missingUsage && (
        <p className="cui__note">
          <InfoCircle width={14} height={14} aria-hidden="true" />
          {coverage.runs_with_usage} of {coverage.runs} turns include usage.
          Active turns update when they finish; older turns may predate tracking.
        </p>
      )}
      <div className="cui-runs">
        {runs.map(run => <RunRow key={run.id} run={run} />)}
      </div>
    </>
  )
}

export default function ChatUsageInspector({ chatId, onClose }) {
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const query = chatQueries.usage.useQuery(chatId, { enabled: !!chatId })

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: onClose ? closeRef : dialogRef,
    onClose,
    closeOnEscape: !!onClose,
  })

  return (
    <div className="cui__overlay" role="presentation" onClick={() => onClose?.()}>
      <div
        ref={dialogRef}
        className="cui"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cui-title"
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="cui__head">
          <div>
            <h2 id="cui-title" className="cui__title">Usage</h2>
            <p className="cui__subtitle">Input, output, cache efficiency, and cost for this chat.</p>
          </div>
          {onClose && (
            <button
              ref={closeRef}
              type="button"
              className="cui__close"
              onClick={onClose}
              aria-label="Close usage"
            >
              <X width={18} height={18} aria-hidden="true" />
            </button>
          )}
        </header>

        <div className="cui__body">
          {query.isLoading ? (
            <p className="cui__state">Loading usage…</p>
          ) : query.isError && !query.data ? (
            <div className="cui__state cui__state--error" role="alert">
              <p>Usage couldn’t load. Check your connection and try again.</p>
              <button
                type="button"
                className="cui__retry"
                onClick={() => query.refetch()}
                disabled={query.isFetching}
              >
                <ArrowRotateCw width={16} height={16} aria-hidden="true" />
                {query.isFetching ? 'Retrying…' : 'Try again'}
              </button>
            </div>
          ) : query.data ? (
            <UsageData data={query.data} />
          ) : null}
        </div>
      </div>
    </div>
  )
}
