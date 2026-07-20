import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { ownerQueries } from '../../hooks/queries.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import './WalkthroughOverlay.css'

const STEPS = ['intro', 'home', 'first-chat']

export default function WalkthroughOverlay({ onDone }) {
  const queryClient = useQueryClient()
  const [stepIdx, setStepIdx] = useState(0)
  const [closing, setClosing] = useState(false)
  const closingRef = useRef(false)
  const dialogRef = useRef(null)
  const step = STEPS[stepIdx]

  function finish() {
    if (closingRef.current) return
    closingRef.current = true
    setClosing(true)
    queryClient.setQueryData(ownerQueries.walkthrough.key, (prev) => ({
      ...(prev || { completed_at: null }),
      completed: true,
    }))
    try { localStorage.setItem('mobius:walkthrough-completed', '1') } catch (_) {}
    api.owner.walkthrough.complete().catch(() => {})
    onDone?.()
  }

  const next = useCallback(() => {
    if (stepIdx < STEPS.length - 1) {
      setStepIdx((s) => s + 1)
    } else {
      finish()
    }
    // finish is guarded by closingRef; stale callbacks cannot complete twice.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepIdx])

  const skip = useCallback(() => {
    finish()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: dialogRef,
    onClose: skip,
  })

  useEffect(() => {
    const standalone =
      (typeof window !== 'undefined' &&
        window.matchMedia &&
        window.matchMedia('(display-mode: standalone)').matches) ||
      (typeof navigator !== 'undefined' && navigator.standalone === true)
    if (standalone) finish()
    // finish is guarded and this should run only once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div
      className={`wt__overlay${closing ? ' wt__overlay--closing' : ''}`}
      role="presentation"
    >
      <div
        ref={dialogRef}
        className={`wt__card wt__card--${step}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="wt-title"
        tabIndex={-1}
      >
        <div className="wt__steps" aria-hidden="true">
          {STEPS.map((s, i) => (
            <span
              key={s}
              className={`wt__step-dot${i === stepIdx ? ' wt__step-dot--active' : ''}${i < stepIdx ? ' wt__step-dot--done' : ''}`}
            />
          ))}
        </div>

        {step === 'intro' && (
          <>
            <div className="wt__sigil" aria-hidden="true">
              <span className="wt__sigil-ring" />
              <span className="wt__sigil-loop" />
            </div>
            <h2 id="wt-title" className="wt__title">This is your agent's home</h2>
            <p className="wt__body">
              You are inside a private Möbius install: chat, apps, code, and data
              in one place. No separate control room. No inside and outside.
            </p>
            <div className="wt__actions">
              <button type="button" className="wt__btn wt__btn--ghost" onClick={skip}>
                Skip
              </button>
              <button type="button" className="wt__btn wt__btn--primary" onClick={next}>
                Next
              </button>
            </div>
          </>
        )}

        {step === 'home' && (
          <>
            <div className="wt__duo" aria-hidden="true">
              <span>Platform</span>
              <span>Apps</span>
              <span>Community</span>
            </div>
            <h2 id="wt-title" className="wt__title">Build the place and the things in it</h2>
            <p className="wt__body">
              Möbius can build apps, install community work, and change the platform
              around them. The Contribute app is where improvements can flow back.
            </p>
            <p className="wt__body wt__body--aside">
              Changing the platform can break it. /recover runs outside Möbius and
              can put it back.
            </p>
            <div className="wt__actions">
              <button type="button" className="wt__btn wt__btn--ghost" onClick={skip}>
                Skip
              </button>
              <button type="button" className="wt__btn wt__btn--primary" onClick={next}>
                Next
              </button>
            </div>
          </>
        )}

        {step === 'first-chat' && (
          <>
            <h2 id="wt-title" className="wt__title">Make the first move</h2>
            <p className="wt__body">
              Start with something small and real: change the theme, build a tiny
              app, or ask Möbius what it can improve in its own home.
            </p>
            <div className="wt__actions">
              <button
                type="button"
                className="wt__btn wt__btn--primary wt__btn--single"
                onClick={next}
              >
                Meet my Möbius
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
