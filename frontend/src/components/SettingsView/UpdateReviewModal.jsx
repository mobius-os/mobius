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
 *   - a failed preview load shows a readable message with Try again + an
 *     "Update anyway" fallback (the owner could already apply without a preview,
 *     so we never take that capability away);
 *   - Not now / tap-outside / Escape leave the instance untouched.
 *
 * Design language mirrors SettingsView + ManageModelsModal: card-on-surface,
 * 1px borders, sentence-case titles, the shared .settings tokens.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { api } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { decodeGitPath, diffFileByPath, parseUnifiedDiff } from '../../lib/parseUnifiedDiff.js'
import {
  fileStatusLabel,
  shortSha,
  summarizePreview,
  isTrivialUpdate,
} from '../../lib/platformUpdatePreview.js'
import DiffView from '../DiffView/DiffView.jsx'
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
  const [openFiles, setOpenFiles] = useState(() => new Set())
  const dialogRef = useRef(null)
  const closeRef = useRef(null)

  const loadPreview = useCallback(async () => {
    setLoading(true)
    setLoadError(false)
    try {
      const res = await api.platform.updatePreview()
      if (!res.ok) throw new Error(`status ${res.status}`)
      setPreview(await res.json())
      setOpenFiles(new Set())
    } catch {
      setLoadError(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadPreview() }, [loadPreview])

  const requestClose = useCallback(() => {
    if (!applying) onClose()
  }, [applying, onClose])

  // Escape and backdrop dismissal remain disabled while an update is applying.
  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: closeRef,
    onClose: requestClose,
    closeOnEscape: !applying,
  })

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
  const { diffByPath, finalDiffPath } = useMemo(() => {
    const parsedDiff = parseUnifiedDiff(preview?.diff)
    return {
      diffByPath: diffFileByPath(parsedDiff),
      finalDiffPath: parsedDiff.at(-1)?.path || null,
    }
  }, [preview?.diff])
  // A preview that resolved to "not available" (e.g. the update landed from
  // another surface between status and open) has nothing to apply.
  const notAvailable = preview && preview.available === false && !loadError

  const summaryBits = []
  if (summary.commitCount) summaryBits.push(commitCountLabel(summary.commitCount))
  if (summary.fileCount) summaryBits.push(fileCountLabel(summary.fileCount))

  const toggleFile = useCallback((path) => {
    setOpenFiles((current) => {
      const next = new Set(current)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }, [])

  return (
    <div
      className="urm__overlay"
      role="presentation"
      onClick={requestClose}
    >
      <div
        ref={dialogRef}
        className="urm"
        role="dialog"
        aria-modal="true"
        aria-labelledby="urm-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="urm__head">
          <h2 id="urm-title" className="urm__title">Review update</h2>
          <button
            ref={closeRef}
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
                  {files.map((file, idx) => {
                    // The backend leaves files[].path git-quoted (a non-ASCII name
                    // stays "caf\303\251.txt"), while the parser's map keys are
                    // decoded — decode here so the lookup matches and the row shows
                    // the real path.
                    const filePath = decodeGitPath(file.path)
                    const isOpen = openFiles.has(filePath)
                    const diffEntry = diffByPath.get(filePath)
                    const hasTextDiff = (diffEntry?.hunks?.length || 0) > 0
                    const diffEndsHere = !!preview?.diff_truncated
                      && finalDiffPath === filePath
                    const diffRegionId = `urm-file-diff-${idx}`
                    return (
                      <li
                        key={filePath}
                        className={`urm__file${isOpen ? ' urm__file--open' : ''}`}
                      >
                        <button
                          type="button"
                          className="urm__file-toggle"
                          onClick={() => toggleFile(filePath)}
                          aria-expanded={isOpen}
                          aria-controls={isOpen ? diffRegionId : undefined}
                        >
                          <span
                            className={`urm__file-badge urm__file-badge--${(file.status || 'M').charAt(0).toLowerCase()}`}
                            title={fileStatusLabel(file.status)}
                          >
                            {(file.status || 'M').charAt(0).toUpperCase()}
                          </span>
                          <span className="urm__file-path">{filePath}</span>
                          <span className="urm__file-stat">
                            {typeof file.insertions === 'number' && file.insertions > 0 && (
                              <span className="urm__stat-add">+{file.insertions}</span>
                            )}
                            {typeof file.deletions === 'number' && file.deletions > 0 && (
                              <span className="urm__stat-del">−{file.deletions}</span>
                            )}
                          </span>
                          <svg
                            className={`urm__file-chevron${isOpen ? ' urm__file-chevron--open' : ''}`}
                            width="10"
                            height="10"
                            viewBox="0 0 10 10"
                            fill="none"
                            aria-hidden="true"
                          >
                            <path
                              d="M2 4l3 3 3-3"
                              stroke="currentColor"
                              strokeWidth="1.5"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            />
                          </svg>
                        </button>
                        {isOpen && (
                          <div className="urm__file-diff" id={diffRegionId}>
                            {!diffEntry || (diffEndsHere && !diffEntry.binary && !hasTextDiff) ? (
                              <p className="urm__file-diff-empty">
                                Diff not shown — this update is large; the full change applies on Apply.
                              </p>
                            ) : (diffEntry.binary || hasTextDiff) ? (
                              <>
                                <DiffView file={diffEntry} />
                                {diffEndsHere && (
                                  <p className="urm__file-diff-note">
                                    Diff truncated here — the full change applies on Apply.
                                  </p>
                                )}
                              </>
                            ) : (
                              <p className="urm__file-diff-empty">
                                No textual changes to preview.
                              </p>
                            )}
                          </div>
                        )}
                      </li>
                    )
                  })}
                </ul>
              </section>
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
