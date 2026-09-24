/** Reviews one immutable update or unfinished activation before the owner commits to it. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { X } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { shortSha, summarizePreview } from '../../lib/platformUpdatePreview.js'
import { deploymentKindLabel, platformActivationLabel, reviewedUpdateUsesContainerRebuild } from '../../lib/platformUpdateState.js'
import UnifiedDiff from '../DiffView/UnifiedDiff.jsx'
import UpdateRepairAction from './UpdateRepairAction.jsx'
import { platformUpdateRepairReason } from '../../lib/platformUpdateRepair.js'
import './UpdateReviewModal.css'

const UPDATE_PHASE_LABELS = {
  preparing: 'Preparing the update…', fetching: 'Getting the reviewed version…',
  reconciling: 'Combining the update with your local changes…',
  validating: 'Checking the update…', building: 'Getting everything ready…',
  finalizing: 'Finishing the update…',
}

function blockingPathLabel(path) {
  if (path === 'Dockerfile') return 'Tools and versions included with Möbius'
  if (path.startsWith('backend/scripts/seed-skills/')) return 'Built-in skill template — not the installed skill'
  if (['backend/requirements.txt', 'backend/requirements.lock'].includes(path)) {
    return 'Python packages included with Möbius'
  }
  if (path.startsWith('backend/runtime/')) return 'Protected startup files'
  if (path.includes('entrypoint')) return 'Startup behavior'
  return 'System setup'
}

export default function UpdateReviewModal({
  intent = 'update', platform, rebuild, onClose, onApply, onRebuild, onResolve,
  applying, rebuilding, resolving, observing, applyError, applyErrorCode, onRefreshReview, applyProgress,
  restoreFocusRef, inertBoundaryRef,
}) {
  const [preview, setPreview] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [resultState, setResultState] = useState('')
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const resultActionRef = useRef(null)
  const applyAttemptedRef = useRef(false)
  const inFlight = applying || rebuilding || resolving
  const busy = inFlight || observing

  const loadPreview = useCallback(async () => {
    setLoading(true); setLoadError(''); setPreview(null); onRefreshReview?.()
    try {
      const response = await api.platform.updatePreview({ intent })
      const body = await response.json().catch(() => null)
      if (!response.ok) {
        const detail = body?.detail
        throw new Error(detail?.message || (typeof detail === 'string' && detail) || 'Couldn’t verify this update.')
      }
      if (typeof body?.actionable !== 'boolean' || !Array.isArray(body?.activation?.required_actions)) {
        throw new Error('The server has not loaded these update controls yet. Close this review and use Restart server in Settings.')
      }
      setPreview(body)
    } catch (error) { setLoadError(error.message || 'Couldn’t verify this update.') }
    finally { setLoading(false) }
  }, [intent, onRefreshReview])
  useEffect(() => { loadPreview() }, [loadPreview])

  const requestClose = useCallback(() => { if (!inFlight) onClose() }, [inFlight, onClose])
  useDialogFocus({ containerRef: dialogRef, initialFocusRef: closeRef, restoreFocusRef,
    onClose: requestClose, closeOnEscape: !inFlight, modal: false, lockScroll: false,
    inertBoundaryRef })

  async function handleApply() {
    applyAttemptedRef.current = true
    const plan = { plan_id: preview.plan_id, current_sha: preview.current_sha,
      target_sha: preview.target_sha, image_digest: preview.image_digest }
    const result = await (reviewedUpdateUsesContainerRebuild(preview) ? onRebuild(plan) : onApply(plan))
    if (result?.state === 'conflict' || result?.state === 'rolled_back') setResultState(result.state)
    // The request owner returns ok only for an explicit accepted domain outcome.
    // HTTP success alone never closes this review.
    else if (result?.ok) onClose()
  }
  const summary = summarizePreview(preview)
  const target = shortSha(preview?.target_sha)
  const commits = preview?.commits || []
  const files = preview?.files || []
  const activation = preview?.activation
  const rebuildUpdate = reviewedUpdateUsesContainerRebuild(preview)
  const finish = preview?.operation === 'finish' || intent === 'finish'
  const hasResult = ['conflict', 'rolled_back'].includes(resultState)
  const hasPlan = !!(preview?.plan_id && preview?.current_sha && preview?.target_sha)
  const actionable = preview?.actionable
  const containerBlockers = preview?.blocking_paths?.length > 0
  // The review proved there is nothing to apply. A leftover rolled_back flag
  // must not turn this into a "needs repair" offer — that is the contradictory
  // "already complete + agent repair" state. Show a single Done instead.
  const nothingToApply = !!preview && actionable === false && !hasResult
  const progressLabel = observing ? 'Confirming the request. No second update will be sent…' : rebuilding ? 'Starting the reviewed system update…'
    : (applyProgress?.plan_id === preview?.plan_id && UPDATE_PHASE_LABELS[applyProgress?.phase]) || 'Preparing the update…'
  const needsRestart = activation?.level === 'server_restart'
  const repairReason = (resultState === 'conflict' || nothingToApply) ? null : platformUpdateRepairReason({
    preview, platform: { ...platform, state: resultState || platform?.state }, error: applyError, errorCode: applyErrorCode,
  })
  useEffect(() => {
    // Result and repair actions can be rendered while the request owner is
    // still settling its phase. Wait for the action to become enabled before
    // moving focus; otherwise focusing a disabled button is a no-op and the
    // pane loses its recovery affordance.
    if (applyAttemptedRef.current && !busy && (resultState || repairReason)) {
      resultActionRef.current?.focus({ preventScroll: true })
    }
  }, [busy, repairReason, resultState])

  return (
    <div className="urm__overlay" role="presentation" onClick={requestClose}>
      <div ref={dialogRef} className="urm" role="dialog" aria-modal="false" aria-labelledby="urm-title"
        tabIndex={-1} onClick={event => event.stopPropagation()}>
        <div className="urm__head">
          <h2 id="urm-title" className="urm__title">{hasResult
            ? (resultState === 'conflict' ? 'Update not applied' : 'Update rolled back')
            : finish ? 'Finish update' : 'Review update'}</h2>
          <button ref={closeRef} type="button" className="urm__close" onClick={requestClose} aria-label="Close" disabled={inFlight}>
            <X width={20} height={20} />
          </button>
        </div>
        <div className="urm__body">
          {hasResult ? (
            <div className="urm__notice" role="status">
              <strong>{resultState === 'conflict' ? 'Your current version is still running.' : 'Your earlier source was restored.'}</strong>
              <p>{resultState === 'conflict'
                ? 'Your local changes overlap this update. Resolve the overlap in chat when you’re ready.'
                : 'The update did not pass its checks. Your chats, apps, and saved changes are still there. Review the details before trying again.'}</p>
            </div>
          ) : loading ? <p className="urm__notice" role="status">Checking this update…</p>
            : loadError ? <p className="urm__notice" role="status">{loadError}</p>
              : !actionable ? <p className="urm__notice" role="status">There’s nothing to apply. This update is already complete.</p>
                : <>
                  <section className="urm__overview">
                    <h3>{repairReason ? 'This update needs help' : finish ? 'Make the installed update active' : 'Update Möbius'}</h3>
                    <p>{containerBlockers
                      ? 'This update would remove changes made to how Möbius runs.'
                      : repairReason || (finish ? 'Finish setting up the installed update.'
                      : 'Install this reviewed update while keeping your changes. If anything overlaps, Möbius pauses and asks you to resolve it.')}</p>
                    <h3>What to expect</h3>
                    <p>{containerBlockers
                      ? 'Möbius will keep running as it is. An agent can compare these changes and prepare an official or custom update that keeps the behavior you need.'
                      : repairReason
                      ? 'Open a chat with the update details included. Möbius will check what’s needed and help finish the update, asking before any restart.'
                      : rebuildUpdate
                      ? 'Möbius will briefly go offline while the updated system starts. Active chats pause and resume when ready, and this page reconnects automatically.'
                      : needsRestart
                        ? 'The update is prepared now. A separate restart makes it active, so you can keep working and combine more updates first.'
                        : 'The interface is rebuilt or changes take effect when next used. No server restart is needed.'}</p>
                    {rebuildUpdate && !repairReason && <p>If the updated system does not pass its checks, Möbius tries to restore the previous system image. Your chats, apps, data, and newly installed source stay in place, so that combination may still need attention.</p>}
                  </section>
                  <details className="urm__technical">
                    <summary>Technical details{summary.fileCount ? ` · ${summary.fileCount} files` : ''}</summary>
                    <p>Reviewed version <code className="urm__sha">{target}</code> · {activation && deploymentKindLabel(activation)}</p>
                    {activation && <p>{platformActivationLabel(activation)}</p>}
                    {containerBlockers && <>
                      <h3>Local system changes to keep</h3>
                      <p>These files differ from the reviewed update. Replacing the running system now would remove what they do.</p>
                      <ul>{preview.blocking_paths.map(path => <li key={path}><strong>{blockingPathLabel(path)}</strong><br /><code>{path}</code></li>)}</ul>
                      {preview?.blocking_diff && <section>
                        <h3>Exact local difference</h3>
                        <UnifiedDiff diff={preview.blocking_diff}
                          summaryOverrides={preview.blocking_paths.map(path => ({ path, status: 'M', insertions: null, deletions: null }))}
                          diffTruncated={!!preview.blocking_diff_truncated} />
                      </section>}
                    </>}
                    {(activation?.guidance || []).map(line => <p key={line}>{line}</p>)}
                    {(activation?.reasons || []).length > 0 && <ul>{activation.reasons.map(reason => <li key={reason.code}>{reason.summary}</li>)}</ul>}
                    {commits.length > 0 && <section>
                      <h3>{summary.commitCount} commits</h3>
                      {summary.commitsTruncated && <p>Showing the newest {commits.length}.</p>}
                      <ul className="urm__commits">{commits.map(commit => <li key={commit.sha}>
                        <code className="urm__sha">{shortSha(commit.sha)}</code> {commit.subject}
                      </li>)}</ul>
                    </section>}
                    {files.length > 0 && <UnifiedDiff diff={preview?.diff} summaryOverrides={files} diffTruncated={!!preview?.diff_truncated} />}
                  </details>
                </>}
          {busy && <p className="urm__notice" role="status">{progressLabel}</p>}
        </div>
        {applyError && <div className="urm__error">{repairReason
          ? <details><summary>Failure details</summary><p>{applyError}</p></details>
          : <Alert color="danger" variant="soft" description={applyError} />}</div>}
        <div className="urm__foot">
          {!nothingToApply && <button type="button" className="settings__btn settings__btn--sm settings__btn--outline" onClick={requestClose} disabled={inFlight}>{observing ? 'Keep working' : 'Not now'}</button>}
          {nothingToApply ? <button ref={resultActionRef} type="button" className="settings__btn settings__btn--sm" onClick={requestClose} disabled={busy}>Done</button>
          : repairReason ? <>
            <UpdateRepairAction preview={preview} platform={{ ...platform, state: resultState || platform?.state }} rebuild={rebuild} error={applyError} errorCode={applyErrorCode} disabled={busy || loading} buttonRef={resultActionRef} className="settings__btn settings__btn--sm" label="Fix with an agent" />
          </>
          : hasResult ? <button ref={resultActionRef} type="button" className="settings__btn settings__btn--sm"
            onClick={resultState === 'conflict' ? onResolve : requestClose} disabled={busy}>
            {resultState === 'conflict' ? (resolving ? 'Opening…' : 'Resolve in chat') : 'Done'}
          </button> : (loadError || ['update_plan_stale', 'update_plan_invalid', 'activation_changed'].includes(applyErrorCode)) ? <button type="button" className="settings__btn settings__btn--sm" onClick={loadPreview} disabled={busy}>{loadError ? 'Try again' : 'Refresh review'}</button>
            : <button type="button" className="settings__btn settings__btn--sm" onClick={handleApply} disabled={busy || loading || !actionable || !hasPlan}>
              {busy ? 'Updating…' : rebuildUpdate ? 'Update now' : 'Apply update'}
            </button>}
        </div>
      </div>
    </div>
  )
}
