/** One next action for updating Möbius, with visible versions and a direct restart control. */
import { useEffect, useRef, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { platformUpdateStatusLabel, platformActivationLevel, reviewedUpdateUsesContainerRebuild } from '../../lib/platformUpdateState.js'
import { rebuildIsActive, rebuildProgressMessage } from '../../lib/containerRebuild.js'
import { containerVersionIdentity, platformVersionIdentity } from '../../lib/platformVersionIdentity.js'
import { formatUpstreamCommitDate } from '../../lib/platformProvenance.js'
import usePlatformUpdates from './usePlatformUpdates.js'
import UpdateReviewModal from './UpdateReviewModal.jsx'
import UpdateRepairAction from './UpdateRepairAction.jsx'
import { platformUpdateRepairReason } from '../../lib/platformUpdateRepair.js'
import './PlatformUpdates.css'

export default function PlatformUpdates({ active, refreshToken, onOpenChat }) {
  const update = usePlatformUpdates({ active, refreshToken, onOpenChat })
  const { platform, rebuild, version, phase, busy } = update
  const [review, setReview] = useState(null)
  const [confirmRestart, setConfirmRestart] = useState(false)
  const actionRef = useRef(null)
  const restoreFocus = useRef(false)
  const level = platformActivationLevel(platform)
  const restartNeeded = ['server_restart', 'dependency_sync'].includes(level)
  const imageNeeded = reviewedUpdateUsesContainerRebuild(platform)
  const conflict = platform?.state === 'conflict'
  const available = platform?.available || platform?.newer_updates_available
  const unavailable = !platform || platform.status_unavailable
  const activeRebuild = rebuildIsActive(rebuild)
  const mobiusVersion = platformVersionIdentity(platform, version)
  const containerVersion = containerVersionIdentity(version)
  const repairReason = !conflict && platformUpdateRepairReason({ platform, rebuild, error: update.error, errorCode: update.errorCode })

  useEffect(() => {
    if (review || busy || !restoreFocus.current) return
    const frame = requestAnimationFrame(() => {
      if (!actionRef.current || actionRef.current.disabled) return
      restoreFocus.current = false
      actionRef.current.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [review, busy, platform, confirmRestart])

  function openReview(intent = 'update') {
    update.clearError()
    setConfirmRestart(false)
    setReview(intent)
  }
  function closeReview() { restoreFocus.current = true; setReview(null) }
  async function check() {
    await update.check()
    restoreFocus.current = true
    // State from the check and focus restoration settle in the same render.
    actionRef.current?.focus({ preventScroll: true })
  }
  function askRestart() { update.clearError(); setConfirmRestart(true) }

  const primary = conflict
    ? { label: platform?.conflict_chat_id ? 'Open chat' : 'Resolve in chat', act: update.resolve }
    : available
      ? { label: 'Review update', act: () => openReview() }
      : imageNeeded
        ? { label: 'Finish update', act: () => openReview('finish') }
        : restartNeeded
          ? { label: 'Restart to finish', act: askRestart }
          : { label: phase === 'checking' ? 'Checking…' : 'Check for updates', act: check }
  const status = activeRebuild ? rebuildProgressMessage(rebuild)
    : update.reconnecting ? (update.observingKind === 'apply' ? 'Checking the update…' : 'Restarting Möbius…')
      : !platform ? 'Checking update status…' : platformUpdateStatusLabel(platform)

  return (
    <section className="settings__section platform-updates" aria-labelledby="platform-updates-title">
      <div className="platform-updates__heading">
        <h2 id="platform-updates-title" className="platform-updates__title">Updates</h2>
        <p className="platform-updates__status" role="status">{status}</p>
      </div>
      {confirmRestart ? (
        <div className="platform-updates__confirmation" role="group" aria-label="Confirm restart">
          <p>Restarting briefly interrupts active chats across Möbius. The page will reconnect automatically. This does not replace the container.</p>
          <div className="platform-updates__actions">
            <button ref={actionRef} className="settings__btn settings__btn--sm" onClick={update.restart} disabled={busy}>
              {busy ? 'Restarting…' : 'Restart now'}
            </button>
            <button className="settings__btn settings__btn--outline settings__btn--sm" disabled={busy} onClick={() => {
              setConfirmRestart(false); restoreFocus.current = true
            }}>Not now</button>
          </div>
        </div>
      ) : (
        <div className="platform-updates__actions">
          <button ref={actionRef} className={`settings__btn settings__btn--sm${!conflict && !available && !imageNeeded && !restartNeeded ? ' settings__btn--outline' : ''}`} disabled={busy || (conflict && !onOpenChat)} onClick={primary.act}>
            {busy ? (phase === 'checking' ? 'Checking…' : 'Updating…') : primary.label}
          </button>
          {available && !conflict && (imageNeeded || restartNeeded) && (
            <button className="settings__btn settings__btn--outline settings__btn--sm" disabled={busy} onClick={imageNeeded ? () => openReview('finish') : askRestart}>
              {imageNeeded ? 'Finish installed update' : 'Restart to finish'}
            </button>
          )}
          {conflict && platform?.newer_updates_available && (
            <button className="settings__btn settings__btn--outline settings__btn--sm" disabled={busy} onClick={() => openReview()}>Review all updates</button>
          )}
          {!available && !conflict && (restartNeeded || imageNeeded) && (
            <button className="settings__btn settings__btn--outline settings__btn--sm" disabled={busy} onClick={check}>Check for more</button>
          )}
          <button className="settings__btn settings__btn--outline settings__btn--sm platform-updates__restart" disabled={busy || confirmRestart} onClick={askRestart}>Restart server</button>
        </div>
      )}
      <dl className="platform-updates__versions">
        <dt>Installed update</dt><dd>{formatUpstreamCommitDate(platform?.contained_upstream_committed_at) || 'Unknown'} {mobiusVersion.primarySha && <code>{mobiusVersion.primarySha}</code>}</dd>
        <dt>Current system</dt><dd>{formatUpstreamCommitDate(version?.build_date) || 'Unknown'} {containerVersion.sha && <code>{containerVersion.sha}</code>}</dd>
      </dl>
      {!busy && !unavailable && !conflict && restartNeeded && (
        <p className="platform-updates__description">Your changes are ready. You can add more updates before restarting once.</p>
      )}
      {activeRebuild && rebuild.status_unavailable && (
        <p className="platform-updates__description">Reconnecting to the update controller. Your update continues outside this page.</p>
      )}
      {update.reconnecting && (
        <p className="platform-updates__description" role="status">{update.slow
          ? 'Taking longer than usual. Still checking; there is no need to restart again.'
          : 'The page will refresh when Möbius is ready.'}</p>
      )}
      {repairReason && !review && !update.reconnecting && (
        <div className="platform-updates__description">
          <p>{repairReason}</p>
          <UpdateRepairAction platform={platform} rebuild={rebuild} error={update.error} errorCode={update.errorCode} disabled={busy} />
          <details><summary>Technical details</summary>
            {update.error && <p>{update.error}</p>}
            {platform?.rollback_error && <p>{platform.rollback_error}</p>}
            {(platform?.activation?.guidance || []).map(line => <p key={line}>{line}</p>)}
          </details>
        </div>
      )}
      {update.checkResult && <p className="platform-updates__description" role="status">{update.checkResult}</p>}
      {!review && !repairReason && update.error && <Alert color="danger" variant="soft" description={update.error} />}
      {review && (
        <UpdateReviewModal intent={review} platform={platform} rebuild={rebuild} onClose={closeReview}
          onApply={plan => update.execute(plan, 'apply')}
          onRebuild={plan => update.execute(plan, 'rebuild')}
          onResolve={update.resolve} applying={phase === 'applying'} rebuilding={phase === 'rebuilding'}
          resolving={phase === 'resolving'} observing={update.reconnecting} applyError={update.error} applyErrorCode={update.errorCode} onRefreshReview={update.clearError} applyProgress={update.progress} />
      )}
    </section>
  )
}
