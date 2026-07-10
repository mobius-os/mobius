/**
 * UpdateReviewModal — the "review the changes before you pull them" sheet for a
 * platform update. Opened from the Settings "Möbius" update row when an update
 * is available, in place of applying immediately.
 *
 * It fetches GET /api/platform/update-preview (read-only, fetch-free on the
 * server) and shows the incoming changes: the target commit, a per-commit list,
 * a per-file summary (status + insertions/deletions), and the raw diff on
 * demand. Two explicit actions: Apply (delegates to the caller's existing
 * platform-apply flow) and Not now (closes, nothing changed).
 *
 * No dead-ends by contract:
 *   - a trivial update (no file changes) shows a one-line confirm, no empty diff
 *     panel;
 *   - a failed preview load shows a readable message with Try again + an
 *     "Update anyway" fallback (the owner could already apply without a preview,
 *     so we never take that capability away);
 *   - Not now / tap-outside / Escape leave the instance untouched.
 *
 * Design language mirrors SettingsView + ManageModelsModal: card-on-surface,
 * 1px borders, sentence-case titles, the shared .settings tokens.
 */

import { useCallback, useEffect, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { api } from '../../api/client.js'
import {
  fileStatusLabel,
  shortSha,
  summarizePreview,
  isTrivialUpdate,
} from '../../lib/platformUpdatePreview.js'
import './UpdateReviewModal.css'

function fileCountLabel(count) {
  return `${count} ${count === 1 ? 'file' : 'files'}`
}

function commitCountLabel(count) {
  return `${count} ${count === 1 ? 'commit' : 'commits'}`
}

export default function UpdateReviewModal({ onClose, onApply, applying, applyError }) {
  const [preview, setPreview] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const [diffOpen, setDiffOpen] = useState(false)

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

  // Escape closes, but never mid-apply — an apply leads to a restart, so we
  // don't want a stray keypress to abandon the sheet while it's in flight.
  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape' && !applying) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [applying, onClose])

  const requestClose = useCallback(() => {
    if (!applying) onClose()
  }, [applying, onClose])

  const handleApply = useCallback(async () => {
    const ok = await onApply()
    // On success the caller advances the row to "Restart to finish"; close so
    // the owner sees that next step. On failure applyError renders in the sheet.
    if (ok) onClose()
  }, [onApply, onClose])

  const summary = summarizePreview(preview)
  const trivial = preview && isTrivialUpdate(preview)
  const target = shortSha(preview?.target_sha)
  const commits = Array.isArray(preview?.commits) ? preview.commits : []
  const files = Array.isArray(preview?.files) ? preview.files : []
  // A preview that resolved to "not available" (e.g. the update landed from
  // another surface between status and open) has nothing to apply.
  const notAvailable = preview && preview.available === false && !loadError

  const summaryBits = []
  if (summary.commitCount) summaryBits.push(commitCountLabel(summary.commitCount))
  if (summary.fileCount) summaryBits.push(fileCountLabel(summary.fileCount))

  return (
    <div
      className="urm__overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="urm-title"
      onClick={requestClose}
    >
      <div className="urm" onClick={(event) => event.stopPropagation()}>
        <div className="urm__head">
          <h2 id="urm-title" className="urm__title">Review update</h2>
          <button
            type="button"
            className="urm__close"
            onClick={requestClose}
            aria-label="Close"
            disabled={applying}
          >×</button>
        </div>

        {!loadError && target && (
          <p className="urm__subtext">
            Updating Möbius to <code className="urm__sha">{target}</code>.
            {summaryBits.length ? ` ${summaryBits.join(' · ')}.` : ''}
          </p>
        )}

        <div className="urm__body">
          {loading && (
            <div className="urm__skeleton" aria-hidden="true">
              <div className="urm__skeleton-row" />
              <div className="urm__skeleton-row" />
              <div className="urm__skeleton-row" />
            </div>
          )}

          {!loading && loadError && (
            <div className="urm__notice" role="status">
              Couldn’t load the change preview. You can try again, or apply the
              update without reviewing it.
            </div>
          )}

          {!loading && !loadError && notAvailable && (
            <div className="urm__notice" role="status">
              This instance is already up to date — there’s nothing to apply.
            </div>
          )}

          {!loading && !loadError && !notAvailable && trivial && (
            <div className="urm__notice" role="status">
              No file changes to review — this update just advances the version.
            </div>
          )}

          {!loading && !loadError && !notAvailable && !trivial && (
            <>
              {commits.length > 0 && (
                <section className="urm__section">
                  <h3 className="urm__section-title">
                    {commitCountLabel(commits.length)}
                  </h3>
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
                <ul className="urm__files">
                  {files.map((file) => (
                    <li key={file.path} className="urm__file">
                      <span
                        className={`urm__file-badge urm__file-badge--${(file.status || 'M').charAt(0).toLowerCase()}`}
                        title={fileStatusLabel(file.status)}
                      >
                        {(file.status || 'M').charAt(0).toUpperCase()}
                      </span>
                      <span className="urm__file-path">{file.path}</span>
                      <span className="urm__file-stat">
                        {typeof file.insertions === 'number' && file.insertions > 0 && (
                          <span className="urm__stat-add">+{file.insertions}</span>
                        )}
                        {typeof file.deletions === 'number' && file.deletions > 0 && (
                          <span className="urm__stat-del">−{file.deletions}</span>
                        )}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>

              {summary.hasDiff && (
                <section className="urm__section">
                  <button
                    type="button"
                    className="urm__diff-toggle"
                    onClick={() => setDiffOpen((open) => !open)}
                    aria-expanded={diffOpen}
                  >
                    {diffOpen ? 'Hide changes' : 'Show changes'}
                  </button>
                  {diffOpen && (
                    <>
                      <pre className="urm__diff"><code>{preview.diff}</code></pre>
                      {summary.diffTruncated && (
                        <p className="urm__diff-note">
                          Diff truncated — the full changes apply when you update.
                        </p>
                      )}
                    </>
                  )}
                </section>
              )}
            </>
          )}
        </div>

        {applyError && (
          <Alert color="danger" variant="soft" description={applyError} />
        )}

        <div className="urm__foot">
          <button
            type="button"
            className="urm__btn urm__btn--ghost"
            onClick={requestClose}
            disabled={applying}
          >
            Not now
          </button>
          {loadError ? (
            <div className="urm__foot-actions">
              <button
                type="button"
                className="urm__btn urm__btn--ghost"
                onClick={loadPreview}
                disabled={applying}
              >
                Try again
              </button>
              <button
                type="button"
                className="urm__btn"
                onClick={handleApply}
                disabled={applying}
              >
                {applying ? 'Applying…' : 'Update anyway'}
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="urm__btn"
              onClick={handleApply}
              disabled={applying || loading || notAvailable}
            >
              {applying ? 'Applying…' : 'Apply update'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
