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

const RESTART_CONFIRM_MIN_MS = 500

export default function PlatformUpdates({ active, refreshToken, onOpenChat, inertBoundaryRef }) {
  const update = usePlatformUpdates({ active, refreshToken, onOpenChat })
  const { platform, cachedPlatform, rebuild, version, phase, busy } = update
  const [review, setReview] = useState(null)
  const [confirmRestart, setConfirmRestart] = useState(null)
  const actionRef = useRef(null)
  const restoreFocus = useRef(false)
  const armedAt = useRef(0)
  const level = platformActivationLevel(platform)
  const restartNeeded = level === 'server_restart'
  const imageNeeded = reviewedUpdateUsesContainerRebuild(platform)
  const conflict = platform?.state === 'conflict'
  const available = platform?.available || platform?.newer_updates_available
  const unavailable = !platform || platform.status_unavailable
  const activeRebuild = rebuildIsActive(rebuild)
  const versionPlatform = platform || cachedPlatform
  const mobiusVersion = platformVersionIdentity(versionPlatform, version)
  const containerVersion = containerVersionIdentity(version)
  const missingVersionLabel = versionPlatform ? 'Unavailable' : 'Checking…'
  const repairReason = !conflict && platformUpdateRepairReason({ platform, rebuild, error: update.error, errorCode: update.errorCode })

  useEffect(() => {
    if (!confirmRestart || busy) return
    const timeout = setTimeout(() => setConfirmRestart(null), 4000)
    return () => clearTimeout(timeout)
  }, [confirmRestart, busy])

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
    setConfirmRestart(null)
    setReview(intent)
  }
  function closeReview() { restoreFocus.current = true; setReview(null) }
  async function check() {
    await update.check()
    restoreFocus.current = true
    // State from the check and focus restoration settle in the same render.
    actionRef.current?.focus({ preventScroll: true })
  }
  // The first press arms that button as its own confirmation; the second restarts.
  // A press right after arming is the same double-click, not a confirmation.
  function pressRestart(source) {
    update.clearError()
    if (confirmRestart !== source) {
      armedAt.current = performance.now()
      return setConfirmRestart(source)
    }
    if (performance.now() - armedAt.current < RESTART_CONFIRM_MIN_MS) return
    setConfirmRestart(null)
    update.restart()
  }

  const primary = conflict
    ? { label: platform?.conflict_chat_id ? 'Open chat' : 'Resolve in chat', act: update.resolve }
    : available
      ? { label: 'Review update', act: () => openReview() }
      : imageNeeded
        ? { label: 'Finish update', act: () => openReview('finish') }
        : restartNeeded
          ? { label: confirmRestart === 'primary' ? 'Confirm restart' : 'Restart to finish', act: () => pressRestart('primary') }
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
      <div className="platform-updates__actions">
        {repairReason ? (
          <UpdateRepairAction platform={platform} rebuild={rebuild} error={update.error} errorCode={update.errorCode}
            disabled={busy} buttonRef={actionRef} className="settings__btn settings__btn--sm" />
        ) : (
          <button ref={actionRef} className={`settings__btn settings__btn--sm${!conflict && !available && !imageNeeded && !restartNeeded ? ' settings__btn--outline' : ''}`} disabled={busy || (conflict && !onOpenChat)} onClick={primary.act}>
            {busy ? (phase === 'checking' ? 'Checking…' : phase === 'restarting' ? 'Restarting…' : 'Updating…') : primary.label}
          </button>
        )}
      </div>
      {repairReason && !review && !update.reconnecting && (
        <div className="platform-updates__description">
          <p>{repairReason}</p>
          {(platform?.activation?.guidance || []).map(line => <p key={line}>{line}</p>)}
        </div>
      )}
      <div className="platform-updates__version-row">
        <dl className="platform-updates__versions">
          <dt>Code</dt><dd>{formatUpstreamCommitDate(versionPlatform?.contained_upstream_committed_at) || missingVersionLabel} {mobiusVersion.primarySha && <code>{mobiusVersion.primarySha}</code>}</dd>
          <dt>Container</dt><dd>{formatUpstreamCommitDate(versionPlatform?.current_build_committed_at || version?.build_date) || missingVersionLabel} {containerVersion.sha && <code>{containerVersion.sha}</code>}</dd>
        </dl>
        <button className="settings__btn settings__btn--outline settings__btn--sm" disabled={busy} onClick={() => pressRestart('dedicated')}>
          {phase === 'restarting' ? 'Restarting…' : confirmRestart === 'dedicated' ? 'Confirm restart' : 'Restart'}
        </button>
      </div>
      {confirmRestart && <p className="platform-updates__description" role="status">Restarting briefly pauses active chats. This page will reconnect automatically. Confirm within 4 seconds, or let this prompt expire.</p>}
      {!busy && !unavailable && !conflict && restartNeeded && (
        <p className="platform-updates__description">Your changes are ready. You can add more updates before restarting once.</p>
      )}
      {activeRebuild && rebuild.status_unavailable && (
        <p className="platform-updates__description">Reconnecting to Möbius. The update is still running.</p>
      )}
      {update.reconnecting && (
        <p className="platform-updates__description" role="status">{update.slow
          ? 'Taking longer than usual. Still checking; there is no need to restart again.'
          : 'The page will refresh when Möbius is ready.'}</p>
      )}
      {update.checkResult && <p className="platform-updates__description" role="status">{update.checkResult}</p>}
      {!review && !repairReason && update.error && <Alert color="danger" variant="soft" description={update.error} />}
      {review && (
        <UpdateReviewModal intent={review} platform={platform} rebuild={rebuild} onClose={closeReview}
          restoreFocusRef={actionRef} inertBoundaryRef={inertBoundaryRef}
          onApply={plan => update.execute(plan, 'apply')}
          onRebuild={plan => update.execute(plan, 'rebuild')}
          onResolve={update.resolve} applying={phase === 'applying'} rebuilding={phase === 'rebuilding'}
          resolving={phase === 'resolving'} observing={update.reconnecting} applyError={update.error} applyErrorCode={update.errorCode} onRefreshReview={update.clearError} applyProgress={update.progress} />
      )}
    </section>
  )
}
