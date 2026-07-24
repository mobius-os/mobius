/**
 * UpdateReviewModal — the "review the changes before you pull them" sheet for a
 * platform update. Opened from the Settings "Möbius" update row when an update
 * is available, in place of applying immediately.
 *
 * It fetches GET /api/platform/update-preview (read-only, fetch-free on the
 * server) and shows the incoming changes: the target commit, a per-commit list,
 * a per-file summary (status + insertions/deletions), and an expandable diff
 * for each file. Two explicit actions: Apply (delegates to the caller's
 * existing platform-apply flow) and Not now (closes, nothing changed).
 *
 * No dead-ends by contract:
 *   - a trivial update (no file changes) shows a one-line confirm, no empty diff
 *     panel;
 *   - a failed preview load shows a readable message with Try again; Apply
 *     requires the immutable plan returned by a successful preview;
 *   - Not now / tap-outside / Escape leave the instance untouched.
 *
 * Design language mirrors SettingsView + ManageModelsModal: card-on-surface,
 * 1px borders, sentence-case titles, the shared .settings tokens.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { api } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { parseUnifiedDiff } from '../DiffView/parseUnifiedDiff.js'
import {
  shortSha,
  summarizePreview,
  isTrivialUpdate,
} from '../../lib/platformUpdatePreview.js'
import FileDiffList from '../DiffView/FileDiffList.jsx'
import './UpdateReviewModal.css'

function fileCountLabel(count) {
  return `${count} ${count === 1 ? 'file' : 'files'}`
}

function commitCountLabel(count) {
  return `${count} ${count === 1 ? 'commit' : 'commits'}`
}

const UPDATE_PHASE_LABELS = {
  preparing: 'Preparing update…',
  fetching: 'Fetching the reviewed release…',
  reconciling: 'Reconciling local changes…',
  validating: 'Validating the backend…',
  building: 'Building the frontend…',
  finalizing: 'Finalizing the update…',
}

export default function UpdateReviewModal({
  onClose,
  onApply,
  onResolve,
  applying,
  resolving,
  applyError,
  applyProgress,
}) {
  const [preview, setPreview] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const [resultState, setResultState] = useState('')
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const resultActionRef = useRef(null)

  const loadPreview = useCallback(async () => {
    setLoading(true)
    setLoadError(false)
    try {
      const res = await api.platform.updatePreview()
      if (!res.ok) throw new Error(`status ${res.status}`)
      setPreview(await res.json())
    } catch {
      setLoadError(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadPreview() }, [loadPreview])

  const requestClose = useCallback(() => {
    if (!applying && !resolving) onClose()
  }, [applying, onClose, resolving])

  // Escape and backdrop dismissal remain disabled while an update or resolver
  // request is in flight.
  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: closeRef,
    onClose: requestClose,
    closeOnEscape: !applying && !resolving,
  })

  const handleApply = useCallback(async () => {
    const result = await onApply({
      plan_id: preview?.plan_id,
      current_sha: preview?.current_sha,
      target_sha: preview?.target_sha,
    })
    // A successful request can still mean the update was blocked. Keep the
    // reviewed sheet open and show that honest outcome in place; only a clean
    // apply closes automatically and advances Settings to its next step.
    if (result?.state === 'conflict' || result?.state === 'rolled_back') {
      setResultState(result.state)
      return
    }
    if (
      result?.ok
      && (result.state === 'restart_needed' || result.state === 'up_to_date')
    ) onClose()
  }, [onApply, onClose, preview])

  // Applying replaces the focused Apply button with a result surface. Focus a
  // real action in that surface so both Tab directions remain inside the
  // dialog; the result notice announces the outcome through role=status.
  useEffect(() => {
    if (resultState) resultActionRef.current?.focus({ preventScroll: true })
  }, [resultState])

  const summary = summarizePreview(preview)
  const trivial = preview && isTrivialUpdate(preview)
  const target = shortSha(preview?.target_sha)
  const commits = Array.isArray(preview?.commits) ? preview.commits : []
  const files = Array.isArray(preview?.files) ? preview.files : []
  const parsedFiles = useMemo(
    () => parseUnifiedDiff(preview?.diff),
    [preview?.diff],
  )
  // A preview that resolved to "not available" (e.g. the update landed from
  // another surface between status and open) has nothing to apply.
  const notAvailable = preview && preview.available === false && !loadError
  const hasResult = resultState === 'conflict' || resultState === 'rolled_back'
  const hasPlan = !!(
    preview?.plan_id && preview?.current_sha && preview?.target_sha
  )
  const progressLabel = (
    applyProgress?.plan_id === preview?.plan_id
      ? UPDATE_PHASE_LABELS[applyProgress?.phase]
      : null
  ) || 'Preparing update…'
  const resultTitle = resultState === 'rolled_back'
    ? 'Update rolled back'
    : 'Update not applied'

  const summaryBits = []
  if (summary.commitCount) summaryBits.push(commitCountLabel(summary.commitCount))
  if (summary.fileCount) summaryBits.push(fileCountLabel(summary.fileCount))

  return (
    <div
      className="urm__overlay"
      role="presentation"
      onClick={requestClose}
    >
      <div
        ref={dialogRef}
        className={`urm${hasResult ? ' urm--result' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="urm-title"
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="urm__head">
          <h2 id="urm-title" className="urm__title">
            {hasResult ? resultTitle : 'Review update'}
          </h2>
          <button
            ref={closeRef}
            type="button"
            className="urm__close"
            onClick={requestClose}
            aria-label="Close"
            disabled={applying || resolving}
          >×</button>
        </div>

        {!hasResult && !loadError && target && (
          <p className="urm__subtext">
            Updating Möbius to <code className="urm__sha">{target}</code>.
            {summaryBits.length ? ` ${summaryBits.join(' · ')}.` : ''}
          </p>
        )}

        <div className="urm__body">
          {hasResult ? (
            <div className="urm__notice urm__notice--result" role="status">
              <strong>
                {resultState === 'rolled_back'
                  ? 'Your previous working version was restored.'
                  : 'Your current version is still running.'}
              </strong>
              <span>
                {resultState === 'rolled_back'
                  ? 'The updated version could not start cleanly, so Möbius rolled it back. The update needs repair before it can land.'
                  : 'Local changes overlap the new version, so Möbius left the working installation untouched. Resolve the overlap in chat when you’re ready.'}
              </span>
            </div>
          ) : loading && (
            <div className="urm__skeleton" aria-hidden="true">
              <div className="urm__skeleton-row" />
              <div className="urm__skeleton-row" />
              <div className="urm__skeleton-row" />
            </div>
          )}

          {!hasResult && !loading && loadError && (
            <div className="urm__notice" role="status">
              Couldn’t load the change preview. Try again to create a fresh,
              immutable update plan.
            </div>
          )}

          {!hasResult && applying && (
            <div className="urm__notice" role="status">
              {progressLabel}
            </div>
          )}

          {!hasResult && !loading && !loadError && notAvailable && (
            <div className="urm__notice" role="status">
              This instance is already up to date — there’s nothing to apply.
            </div>
          )}

          {!hasResult && !loading && !loadError && !notAvailable && trivial && (
            <div className="urm__notice" role="status">
              No file changes to review — this update just advances the version.
            </div>
          )}

          {!hasResult && !loading && !loadError && !notAvailable && !trivial && (
            <>
              {commits.length > 0 && (
                <section className="urm__section">
                  <h3 className="urm__section-title">
                    {commitCountLabel(summary.commitCount)}
                  </h3>
                  {summary.commitsTruncated && (
                    <p className="urm__section-note">
                      Showing the newest {commitCountLabel(commits.length)}.
                    </p>
                  )}
                  <ul className="urm__commits">
                    {commits.map((commit) => (
                      <li key={commit.sha} className="urm__commit">
                        <code className="urm__sha">{shortSha(commit.sha)}</code>
                        <span className="urm__commit-subject">{commit.subject}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              <section className="urm__section">
                <h3 className="urm__section-title">{fileCountLabel(files.length)}</h3>
                <FileDiffList
                  files={parsedFiles}
                  summaryOverrides={files}
                  diffTruncated={!!preview?.diff_truncated}
                />
              </section>
            </>
          )}
        </div>

        {applyError && (
          <div className="urm__error">
            <Alert color="danger" variant="soft" description={applyError} />
          </div>
        )}

        <div className="urm__foot">
          {resultState !== 'rolled_back' && (
            <button
              type="button"
              className="urm__btn urm__btn--ghost"
              onClick={requestClose}
              disabled={applying || resolving}
            >
              Not now
            </button>
          )}
          {resultState === 'conflict' ? (
            <button
              ref={resultActionRef}
              type="button"
              className="urm__btn"
              onClick={onResolve}
              disabled={applying || resolving}
            >
              {resolving ? 'Opening…' : 'Resolve in chat'}
            </button>
          ) : resultState === 'rolled_back' ? (
            <button
              ref={resultActionRef}
              type="button"
              className="urm__btn"
              onClick={requestClose}
            >
              Done
            </button>
          ) : loadError ? (
            <button
              type="button"
              className="urm__btn"
              onClick={loadPreview}
              disabled={applying}
            >
              Try again
            </button>
          ) : (
            <button
              type="button"
              className="urm__btn"
              onClick={handleApply}
              disabled={applying || loading || notAvailable || !hasPlan}
            >
              {applying ? 'Applying…' : 'Apply update'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
