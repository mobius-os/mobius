/**
 * ChatUsageInspector — full token/cost breakdown for one chat, opened from
 * the "Token usage & cost" row in ComposerPopover. Mirrors
 * AgentContextInspector's overlay/dialog shape (same CSS class prefix
 * pattern renamed to `cui`) so the two full-screen chat panels read as one
 * family, but the content is a totals summary plus a per-turn list instead
 * of markdown sections.
 *
 * Backed by GET /chats/{id}/usage, which already existed as an owner-only
 * diagnostic (benchmark tooling reads it too) — this is the first UI surface
 * for it. Historical runs created before usage capture render with "—" for
 * missing fields rather than 0, so the owner can tell "not tracked yet" apart
 * from "genuinely free."
 */

import { useRef } from 'react'
import { InfoCircle } from '@openai/apps-sdk-ui/components/Icon'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { chatQueries } from '../../hooks/queries.js'
import {
  formatCostUsd,
  formatTimestamp,
  formatTokenCount,
} from './chatUsageFormat.js'

const CSS = `
.cui__overlay {
  position: absolute;
  inset: 0;
  z-index: 1000;
  display: grid;
  place-items: center;
  padding: 18px;
  box-sizing: border-box;
  background: rgba(0, 0, 0, 0.45);
}

[data-theme="light"] .cui__overlay {
  background: rgba(15, 18, 25, 0.32);
}

.cui {
  width: min(680px, 100%);
  max-height: calc(100% - 36px);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
  border-radius: 18px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  box-shadow: 0 18px 52px rgba(0, 0, 0, 0.32);
}

[data-theme="light"] .cui {
  box-shadow:
    0 4px 12px rgba(0, 0, 0, 0.08),
    0 1px 3px rgba(0, 0, 0, 0.06);
}

.cui__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 18px;
  border-bottom: 1px solid var(--border);
}

.cui__title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  line-height: 1.25;
}

.cui__subtitle {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

.cui__close {
  flex-shrink: 0;
  border: 0;
  width: 40px;
  height: 40px;
  margin: -7px -7px 0 0;
  border-radius: 9px;
  padding: 0;
  background: none;
  color: var(--muted);
  font: inherit;
  font-size: 24px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}

.cui__close:hover {
  background: var(--surface2);
  color: var(--text);
}

.cui__body {
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 14px;
  overscroll-behavior: contain;
}

.cui__state {
  margin: 0;
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg);
  color: var(--muted);
  font-size: 13px;
  line-height: 1.5;
}

.cui__state--error { color: var(--danger); }

.cui-totals {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 1px;
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
  background: var(--border);
}

.cui-totals__cell {
  background: var(--bg);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.cui-totals__label {
  font-size: 11px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.cui-totals__value {
  font-size: 17px;
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

.cui__coverage {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
  display: flex;
  align-items: center;
  gap: 6px;
}

.cui-runs {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.cui-run {
  border: 1px solid var(--border-light);
  border-radius: 10px;
  background: var(--bg);
  overflow: hidden;
}

.cui-run__summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  cursor: pointer;
  user-select: none;
  list-style: none;
}

.cui-run__summary::-webkit-details-marker { display: none; }

.cui-run__when {
  font-size: 12px;
  color: var(--muted);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cui-run__meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.cui-run__provider {
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--surface2);
  color: var(--muted);
  text-transform: capitalize;
}

.cui-run__cost {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
  min-width: 44px;
  text-align: right;
}

.cui-run__chevron {
  color: var(--muted);
  font-size: 16px;
  line-height: 1;
  transition: transform 0.12s ease;
}

.cui-run[open] .cui-run__chevron { transform: rotate(90deg); }

.cui-run__detail {
  margin: 0;
  border-top: 1px solid var(--border);
  padding: 10px 12px 12px;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 8px 14px;
}

.cui-run__field {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cui-run__field-label {
  font-size: 10.5px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.cui-run__field-value {
  font-size: 13px;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

@media (max-width: 640px) {
  .cui__overlay { align-items: end; padding: 10px; }
  .cui { max-height: calc(100% - 20px); border-radius: 18px; }
  .cui__head { padding: 16px; }
  .cui__body { padding: 10px; }
}
`

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

function RunRow({ run }) {
  const when = formatTimestamp(run.started_at) || 'Unknown time'
  const cost = formatCostUsd(run.cost_usd)
  return (
    <details className="cui-run">
      <summary className="cui-run__summary">
        <span className="cui-run__when">{when}</span>
        <span className="cui-run__meta">
          {run.provider && <span className="cui-run__provider">{run.provider}</span>}
          <span className="cui-run__cost">{cost ?? '—'}</span>
          <span className="cui-run__chevron" aria-hidden="true">›</span>
        </span>
      </summary>
      <div className="cui-run__detail">
        <RunField label="Fresh input" value={formatTokenCount(run.usage?.uncached_input_tokens)} />
        <RunField label="Cached input" value={formatTokenCount(run.cache_read_input_tokens)} />
        <RunField label="Cache write" value={formatTokenCount(run.cache_creation_input_tokens)} />
        <RunField label="Output" value={formatTokenCount(run.output_tokens)} />
        <RunField label="Reasoning" value={formatTokenCount(run.reasoning_output_tokens)} />
        <RunField label="Total tokens" value={formatTokenCount(run.total_tokens)} />
        <RunField label="Model" value={run.usage?.provider_model_usage
          ? Object.keys(run.usage.provider_model_usage)[0]
          : (run.usage?.model || null)} />
        <RunField label="Context window" value={formatTokenCount(run.model_context_window)} />
        <RunField label="Status" value={run.status} />
      </div>
    </details>
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

  const data = query.data
  const totals = data?.totals
  const runs = Array.isArray(data?.runs) ? [...data.runs].reverse() : []
  const coverage = data?.coverage

  return (
    <div className="cui__overlay" role="presentation" onClick={() => onClose?.()}>
      <style>{CSS}</style>
      <div
        ref={dialogRef}
        className="cui"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cui-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="cui__head">
          <div>
            <h2 id="cui-title" className="cui__title">Token usage &amp; cost</h2>
            <p className="cui__subtitle">Per-turn provider usage for this chat.</p>
          </div>
          {onClose && (
            <button
              ref={closeRef}
              type="button"
              className="cui__close"
              onClick={onClose}
              aria-label="Close"
            >×</button>
          )}
        </div>

        <div className="cui__body">
          {query.isLoading && <p className="cui__state">Loading usage…</p>}
          {query.isError && (
            <p className="cui__state cui__state--error">
              {query.error?.message || 'Could not load usage for this chat.'}
            </p>
          )}
          {data && (
            <>
              <div className="cui-totals">
                <TotalsCell label="Total cost" value={formatCostUsd(totals?.cost_usd)} />
                <TotalsCell label="Total tokens" value={formatTokenCount(totals?.total_tokens)} />
                <TotalsCell label="Input tokens" value={formatTokenCount(totals?.input_tokens)} />
                <TotalsCell label="Output tokens" value={formatTokenCount(totals?.output_tokens)} />
                <TotalsCell label="Cached input" value={formatTokenCount(totals?.cache_read_input_tokens)} />
                <TotalsCell label="Reasoning" value={formatTokenCount(totals?.reasoning_output_tokens)} />
              </div>
              {coverage && (
                <p className="cui__coverage">
                  <InfoCircle width={14} height={14} aria-hidden="true" />
                  {coverage.runs_with_usage} of {coverage.runs} turns have usage data
                  {coverage.runs > coverage.runs_with_usage
                    ? ' — older turns predate usage tracking.'
                    : '.'}
                </p>
              )}
              {runs.length === 0 ? (
                <p className="cui__state">No turns recorded yet for this chat.</p>
              ) : (
                <div className="cui-runs">
                  {runs.map(run => <RunRow key={run.id} run={run} />)}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
