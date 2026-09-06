/** One next action for updating Möbius, with visible versions and a direct restart control. */
import { useEffect, useRef, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { platformUpdateStatusLabel, platformActivationLevel } from '../../lib/platformUpdateState.js'
import { rebuildIsActive, rebuildProgressMessage } from '../../lib/containerRebuild.js'
import { containerVersionIdentity, platformVersionIdentity } from '../../lib/platformVersionIdentity.js'
import { formatUpstreamCommitDate } from '../../lib/platformProvenance.js'
import usePlatformUpdates from './usePlatformUpdates.js'
import UpdateReviewModal from './UpdateReviewModal.jsx'
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
  const imageNeeded = level === 'image_rebuild'
  const externalNeeded = !['live', 'server_restart', 'dependency_sync', 'image_rebuild'].includes(level)
  const conflict = platform?.state === 'conflict'
  const available = platform?.available || platform?.newer_updates_available
  const unavailable = !platform || platform.status_unavailable
  const activeRebuild = rebuildIsActive(rebuild)
  const mobiusVersion = platformVersionIdentity(platform, version)
  const containerVersion = containerVersionIdentity(version)

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
            <button ref={actionRef} className="settings__btn" onClick={update.restart} disabled={busy}>
              {busy ? 'Restarting…' : 'Restart now'}
            </button>
            <button className="settings__btn settings__btn--outline" disabled={busy} onClick={() => {
              setConfirmRestart(false); restoreFocus.current = true
            }}>Not now</button>
          </div>
        </div>
      ) : (
        <div className="platform-updates__actions">
          <button ref={actionRef} className={`settings__btn${!conflict && !available && !imageNeeded && !restartNeeded ? ' settings__btn--outline' : ''}`} disabled={busy || (conflict && !onOpenChat)} onClick={primary.act}>
            {busy ? (phase === 'checking' ? 'Checking…' : 'Updating…') : primary.label}
          </button>
          {available && !conflict && (imageNeeded || restartNeeded) && (
            <button className="settings__btn settings__btn--outline" disabled={busy} onClick={imageNeeded ? () => openReview('finish') : askRestart}>
              {imageNeeded ? 'Finish installed update' : 'Restart to finish'}
            </button>
          )}
          {conflict && platform?.newer_updates_available && (
            <button className="settings__btn settings__btn--outline" disabled={busy} onClick={() => openReview()}>Review all updates</button>
          )}
        </div>
      )}
      <dl className="platform-updates__versions">
        <dt>Source code</dt><dd>{formatUpstreamCommitDate(platform?.contained_upstream_committed_at) || 'Unknown'} {mobiusVersion.primarySha && <code>{mobiusVersion.primarySha}</code>}</dd>
        <dt>Container</dt><dd>{formatUpstreamCommitDate(version?.build_date) || 'Unknown'} {containerVersion.sha && <code>{containerVersion.sha}</code>}</dd>
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
      {externalNeeded && (
        <div className="platform-updates__description">
          {(platform?.activation?.guidance || []).map(line => <p key={line}>{line}</p>)}
        </div>
      )}
      {update.checkResult && <p className="platform-updates__description" role="status">{update.checkResult}</p>}
      {!review && update.error && <Alert color="danger" variant="soft" description={update.error} />}
      {platform?.state === 'rolled_back' && !review && (
        <Alert color="warning" variant="soft" description={platform.rollback_error || 'The update did not complete. Your previous source was restored; review the update before trying again.'} />
      )}
      <div className="platform-updates__maintenance">
        {!available && !conflict && (restartNeeded || imageNeeded) && (
          <button className="settings__btn settings__btn--outline" disabled={busy} onClick={check}>Check for more</button>
        )}
        <button className="settings__btn settings__btn--outline" disabled={busy || confirmRestart} onClick={askRestart}>Restart server</button>
      </div>
      {review && (
        <UpdateReviewModal intent={review} onClose={closeReview}
          onApply={plan => update.execute(plan, 'apply')}
          onRebuild={plan => update.execute(plan, 'rebuild')}
          onResolve={update.resolve} applying={phase === 'applying'} rebuilding={phase === 'rebuilding'}
          resolving={phase === 'resolving'} observing={update.reconnecting} applyError={update.error} applyErrorCode={update.errorCode} onRefreshReview={update.clearError} applyProgress={update.progress} />
      )}
    </section>
  )
}
