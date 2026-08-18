/* One publication surface for hosted use and independently installable copies. */

import { useMemo, useRef, useState } from 'react'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import {
  appInstallShareText,
  appNativeSharePayload,
  appShareState,
} from './appShareState.js'
import './ShareAppSheet.css'

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value)
    return true
  } catch {
    return false
  }
}

export default function ShareAppSheet({
  app, apps, onOpenApp, onPublish, onStop, onClose,
}) {
  const cardRef = useRef(null)
  const primaryFocusRef = useRef(null)
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmStop, setConfirmStop] = useState(false)
  const state = useMemo(() => appShareState(app, apps), [app, apps])
  const publication = app.hosted_publication
  const hasUnpublishedChanges = !!publication?.has_unpublished_changes
  const canNativeShare = typeof navigator !== 'undefined' &&
    typeof navigator.share === 'function'
  const publicUrl = typeof window !== 'undefined'
    ? `${window.location.origin}${publication?.path || `/${encodeURIComponent(app.slug)}`}`
    : publication?.path || `/${encodeURIComponent(app.slug)}`

  async function publishHosted() {
    setBusy(true)
    setStatus('')
    try {
      await onPublish?.(app.id)
      setConfirmStop(false)
      setStatus(hasUnpublishedChanges ? 'Published version updated.' : 'Public link is live.')
    } catch (error) {
      setStatus(error?.message || 'Could not update public access.')
    } finally {
      setBusy(false)
    }
  }

  async function stopHosted() {
    setBusy(true)
    setStatus('')
    try {
      await onStop?.(app.id)
      setConfirmStop(false)
      setStatus('Public access stopped.')
    } catch (error) {
      setStatus(error?.message || 'Could not stop public access.')
    } finally {
      setBusy(false)
    }
  }

  async function copyPublicLink() {
    const copied = await copyText(publicUrl)
    setStatus(copied
      ? 'Public link copied.'
      : "Couldn't copy automatically. Press and hold the link to copy it.")
  }

  async function sharePublicLink() {
    setStatus('')
    try {
      await navigator.share({ title: app.name, url: publicUrl })
    } catch (error) {
      if (error?.name !== 'AbortError') {
        setStatus("That share didn't open. You can copy the public link instead.")
      }
    }
  }

  useDialogFocus({
    containerRef: cardRef,
    initialFocusRef: primaryFocusRef,
    onClose,
  })

  async function shareInstallLink() {
    setStatus('')
    try {
      await navigator.share(appNativeSharePayload(app, state.installUrl))
      onClose?.()
    } catch (error) {
      if (error?.name !== 'AbortError') {
        setStatus("That share didn't open. You can copy the install link instead.")
      }
    }
  }

  async function copyInstallLink() {
    const copied = await copyText(appInstallShareText(app, state.installUrl))
    setStatus(copied
      ? 'Install link copied.'
      : "Couldn't copy automatically. Press and hold the link to copy it.")
  }

  function openTarget() {
    if (!state.targetApp) return
    const id = state.targetApp.id
    onClose?.()
    onOpenApp?.(id)
  }

  const installPublished = state.kind === 'published'
  const hasTarget = !!state.targetApp
  const targetIsContribute = state.kind === 'open-contribute'

  return (
    <div className="sas__overlay" onClick={() => onClose?.()}>
      <div
        ref={cardRef}
        className="sas__card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="sas-title"
        tabIndex={-1}
        onClick={event => event.stopPropagation()}
      >
        <div className="sas__identity">
          <img
            className="sas__icon"
            src={`/apps/${encodeURIComponent(app.slug)}/icon-192.png`}
            alt=""
          />
          <div>
            <p className="sas__eyebrow">
              {publication ? 'Hosted publicly' : 'Private app'}
            </p>
            <h2 id="sas-title" className="sas__title">
              Share {app.name}
            </h2>
          </div>
        </div>

        <section className="sas__section" aria-labelledby="sas-public-title">
          <h3 id="sas-public-title" className="sas__section-title">
            {publication
              ? hasUnpublishedChanges ? 'Published version has an update' : 'Public use is on'
              : 'Private to you'}
          </h3>
          <p className="sas__body">
            {publication
              ? hasUnpublishedChanges
                ? 'The existing public version is still live. Publish the update when you are ready; private edits never go live by accident.'
                : 'Anyone with this link can use this exact published version without signing in. They cannot access your chats, files, or app data.'
              : 'Turn on public use to give people a durable link. The app stays sandboxed and your personal data remains private.'}
          </p>
          {publication && (
            <div className="sas__url" tabIndex={0}>{publicUrl}</div>
          )}
          {confirmStop ? (
            <div className="sas__confirm" role="group" aria-label="Confirm stopping public access">
              <p>Existing public sessions will stop working immediately.</p>
              <div className="sas__actions">
                <button
                  type="button"
                  className="sas__btn sas__btn--secondary"
                  onClick={() => setConfirmStop(false)}
                  disabled={busy}
                >
                  Keep public
                </button>
                <button
                  type="button"
                  className="sas__btn sas__btn--danger"
                  onClick={stopHosted}
                  disabled={busy}
                >
                  {busy ? 'Stopping…' : 'Stop public access'}
                </button>
              </div>
            </div>
          ) : (
            <div className="sas__actions">
              {publication ? (
                <>
                  {hasUnpublishedChanges && (
                    <button
                      ref={primaryFocusRef}
                      type="button"
                      className="sas__btn sas__btn--primary"
                      onClick={publishHosted}
                      disabled={busy}
                    >
                      {busy ? 'Publishing…' : 'Publish update'}
                    </button>
                  )}
                  {canNativeShare && (
                    <button
                      ref={hasUnpublishedChanges ? undefined : primaryFocusRef}
                      type="button"
                      className="sas__btn sas__btn--primary"
                      onClick={sharePublicLink}
                    >
                      Share link
                    </button>
                  )}
                  <button
                    ref={canNativeShare || hasUnpublishedChanges ? undefined : primaryFocusRef}
                    type="button"
                    className={`sas__btn ${canNativeShare ? 'sas__btn--secondary' : 'sas__btn--primary'}`}
                    onClick={copyPublicLink}
                  >
                    Copy link
                  </button>
                  <button
                    type="button"
                    className="sas__btn sas__btn--quiet"
                    onClick={() => setConfirmStop(true)}
                  >
                    Stop
                  </button>
                </>
              ) : (
                <button
                  ref={primaryFocusRef}
                  type="button"
                  className="sas__btn sas__btn--primary"
                  onClick={publishHosted}
                  disabled={busy}
                >
                  {busy ? 'Making public…' : 'Make public'}
                </button>
              )}
            </div>
          )}
        </section>

        <section className="sas__section sas__section--install" aria-labelledby="sas-install-title">
          <h3 id="sas-install-title" className="sas__section-title">Install or remix</h3>
        {installPublished ? (
          <>
            <p className="sas__body">
              Send this to another Möbius owner. They get an editable copy of
              the app with fresh data of their own.
            </p>
            <div className="sas__url" tabIndex={0}>{state.installUrl}</div>
            <div className="sas__actions">
              {canNativeShare && (
                <button
                  type="button"
                  className="sas__btn sas__btn--primary"
                  onClick={shareInstallLink}
                >
                  Share install link
                </button>
              )}
              <button
                type="button"
                className={`sas__btn ${canNativeShare ? 'sas__btn--secondary' : 'sas__btn--primary'}`}
                onClick={copyInstallLink}
              >
                Copy install link
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="sas__body">
              {targetIsContribute
                ? 'Contribute can help publish this app, giving it an install link you can share.'
                : state.kind === 'install-contribute'
                  ? 'Install Contribute from the App Store, then use it to publish this app and create a shareable install link.'
                  : 'Install Contribute from the Möbius App Store, then use it to publish this app.'}
            </p>
            <div className="sas__route">
              <span className="sas__route-step">1</span>
              <span>{targetIsContribute ? 'Open Contribute' : 'Install Contribute'}</span>
              <span className="sas__route-line" aria-hidden="true" />
              <span className="sas__route-step">2</span>
              <span>Publish and share</span>
            </div>
            <div className="sas__actions">
              {hasTarget ? (
                <button
                  type="button"
                  className="sas__btn sas__btn--primary"
                  onClick={openTarget}
                >
                  {targetIsContribute ? 'Open Contribute' : 'Open App Store'}
                </button>
              ) : (
                <button
                  type="button"
                  className="sas__btn sas__btn--secondary"
                  onClick={() => onClose?.()}
                >
                  Close
                </button>
              )}
            </div>
          </>
        )}
        </section>

        {status && (
          <p className="sas__status" role="status">{status}</p>
        )}
      </div>
    </div>
  )
}
