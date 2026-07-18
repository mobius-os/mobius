import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { ownerQueries } from '../../hooks/queries.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import {
  copyOriginUrl,
  detectInstallPlatform,
  installCopyForPlatform,
} from '../../utils/installPlatform.js'
import { WORKSPACE_SPLITS_ENABLED } from '../Shell/paneModel.js'
import { insertWorkspaceStep } from '../Shell/workspaceOnboarding.js'
import './WalkthroughOverlay.css'

// First-sign-in walkthrough. Renders as a centered modal styled to
// match the sub-app install card (see standalone.py). Three steps,
// each Next-able; the last step marks completion server-side via
// POST /api/owner/walkthrough/complete and never shows again.
//
// Why centered-modal: the user explicitly asked for it on the install
// card and asked for the walkthrough to feel consistent with that.
//
// Why we don't try to auto-`prompt()` install: at first-sign-in time
// Möbius has barely been touched, so on Chromium the engagement
// counter hasn't reached the BIP threshold yet. Surfacing an Install
// button that the browser won't honor is worse than telling the user
// honestly where the menu lives. Same logic that drives the
// suppression-aware copy on the sub-app card.

// On iOS-non-Safari (Chrome on iOS, Firefox on iOS, etc.) PWA install
// is impossible without switching to Safari first. Showing the
// "Add to home screen" step there is a dead end. We skip the step
// entirely. If the user later opens Möbius in Safari they'll naturally
// encounter the install affordances through the standalone-shell
// install card (a separate, drawer-triggered surface).
//
// Step order is deliberate: orient → empower → install →
// safety-net → CTA. The "customize" and "safety-net" steps were
// added on 2026-05-30 because the agent's write access (covers
// theme, shell behavior, building apps) and the recovery flow are
// the two things a new user genuinely cannot discover by tapping
// around — everything else is visible from the drawer.
// The 'workspace' step (drag-to-split) is inserted after 'customize' only when
// the splits flag is on (design §7.1) — teaching a gesture the flag-off build
// can't perform would mislead. insertWorkspaceStep is a no-op when off.
const STEPS_FULL = insertWorkspaceStep(
  ['intro', 'customize', 'install', 'safety-net', 'first-chat'], WORKSPACE_SPLITS_ENABLED,
)
const STEPS_NO_INSTALL = insertWorkspaceStep(
  ['intro', 'customize', 'safety-net', 'first-chat'], WORKSPACE_SPLITS_ENABLED,
)

export default function WalkthroughOverlay({ onDone }) {
  const queryClient = useQueryClient()
  const [stepIdx, setStepIdx] = useState(0)
  // Cosmetic flag that drives the fade-out CSS; the ACTUAL guard
  // against double-finish lives in closingRef so memoized callbacks
  // see the latest value across renders.
  const [closing, setClosing] = useState(false)
  const closingRef = useRef(false)
  const [platform] = useState(() => detectInstallPlatform())
  const [installCopy] = useState(() => installCopyForPlatform(platform))
  // A coarse pointer gets the hold-to-lift copy; a fine pointer, drag-to-edge.
  const [coarsePointer] = useState(
    () => typeof matchMedia !== 'undefined' && matchMedia('(pointer: coarse)').matches,
  )
  // Tracks whether copy-link succeeded so we can surface a confirm
  // toast on the iOS-non-Safari path.
  const [copyState, setCopyState] = useState(null)
  const dialogRef = useRef(null)

  const STEPS = platform.iosNonSafari ? STEPS_NO_INSTALL : STEPS_FULL
  const step = STEPS[stepIdx]

  // `finish` is captured by callbacks below via the function-decl
  // hoisting, but the guard MUST live in a ref — a useState `closing`
  // is only visible to closures that re-render after the set, while
  // double-tap and double-Esc both fire synchronously inside the same
  // render's callback bodies.
  function finish() {
    if (closingRef.current) return
    closingRef.current = true
    setClosing(true)
    // Optimistic cache update so Shell.jsx's
    // `walkthroughQuery.data.completed` flips to true the same render,
    // unmounting WalkthroughOverlay immediately. Without this the
    // overlay sits in DOM at opacity:0 for the network round-trip and
    // can swallow the user's first tap on the chat input.
    queryClient.setQueryData(ownerQueries.walkthrough.key, (prev) => ({
      ...(prev || { completed_at: null }),
      completed: true,
    }))
    // localStorage fallback: when the POST below fails on a flaky
    // connection, the in-memory query cache is the ONLY record that
    // the user dismissed. Logging out (or clearing the TanStack cache)
    // would then re-show the walkthrough next sign-in even though the
    // user already finished it once. Writing here gives the on-mount
    // gate in Shell.jsx an eventually-consistent second source — see
    // the walkthrough query function which OR-s server + localStorage.
    try { localStorage.setItem('mobius:walkthrough-completed', '1') } catch (_) {}
    // Best-effort persist to the server. On network failure the
    // localStorage flag above keeps the user from re-onboarding;
    // the next successful POST (next session attempt or manual
    // dismiss) reconciles.
    api.owner.walkthrough.complete().catch(() => {})
    onDone?.()
  }

  const next = useCallback(() => {
    if (stepIdx < STEPS.length - 1) {
      setStepIdx((s) => s + 1)
    } else {
      finish()
    }
    // `finish` is defined above and read at call time. The ref-based
    // guard inside `finish` makes the stale-closure concern moot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepIdx, STEPS.length])

  const skip = useCallback(() => {
    // Skip is a kind of completion — we still mark the walkthrough
    // done so the user isn't re-walked next sign-in. They've at
    // least seen the intro; nagging is worse than letting it go.
    finish()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: dialogRef,
    onClose: skip,
  })

  // Already installed → complete silently. If the shell is running as a
  // standalone PWA, the user has clearly installed Möbius, so the
  // walkthrough (and especially its install step) is moot. More
  // importantly this clears the sticky-overlay failure: the overlay is
  // position:fixed/inset:0/z-index:1000 and only unmounts on finish();
  // a user who installed and relaunched would otherwise stare through a
  // covered shell until app restart. finish() unmounts it now and marks
  // the walkthrough complete (correct: a real standalone launch means a
  // real install). The guard lives ONLY here on mount — fetchWalkthrough's
  // completion OR-logic is deliberately untouched, so we don't reshape
  // the broader gating for an emulated-standalone non-installed user.
  useEffect(() => {
    const standalone =
      (typeof window !== 'undefined' &&
        window.matchMedia &&
        window.matchMedia('(display-mode: standalone)').matches) ||
      (typeof navigator !== 'undefined' && navigator.standalone === true)
    if (standalone) finish()
    // finish is a stable function decl guarded by closingRef; running
    // once on mount is the intent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function handleInstallCta() {
    if (installCopy.unsupported && platform.iosNonSafari) {
      const ok = await copyOriginUrl()
      setCopyState(ok ? 'copied' : 'failed')
      // Auto-clear the indicator after a few seconds so it doesn't
      // linger past the user's next interaction.
      setTimeout(() => setCopyState(null), 2500)
      // Don't auto-advance — the user still needs to switch apps.
      return
    }
    next()
  }

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
            {/* "Quick tour" avoids the title-collision with SetupWizard's
                "Welcome to Möbius" heading — for a brand-new owner the
                walkthrough renders moments after setup completes, and
                seeing the same greeting twice reads as a regression. */}
            <h2 id="wt-title" className="wt__title">Quick tour</h2>
            <p className="wt__body">
              Möbius is a chat surface in front of a coding agent. It
              can build mini-apps and modify the platform itself. Tell
              it what you want, and it writes the code.
            </p>
            <div className="wt__actions">
              <button
                type="button"
                className="wt__btn wt__btn--ghost"
                onClick={skip}
              >
                Skip
              </button>
              <button
                type="button"
                className="wt__btn wt__btn--primary"
                onClick={next}
              >
                Next
              </button>
            </div>
          </>
        )}

        {step === 'customize' && (
          <>
            <h2 id="wt-title" className="wt__title">Make it yours</h2>
            <p className="wt__body">
              The agent has write access to almost everything: the theme,
              the shell UI, the drawer, the apps you install or build,
              even how it talks to you. Ask in chat — "switch to a
              light theme," or "remove the voice button" — and it
              edits the code and shows you the result.
            </p>
            <div className="wt__actions">
              <button
                type="button"
                className="wt__btn wt__btn--ghost"
                onClick={skip}
              >
                Skip
              </button>
              <button
                type="button"
                className="wt__btn wt__btn--primary"
                onClick={next}
              >
                Next
              </button>
            </div>
          </>
        )}

        {step === 'workspace' && (
          <>
            <h2 id="wt-title" className="wt__title">Work side by side</h2>
            {/* A small looping mock of a tab dragging to an edge and snapping
                into a split. Under prefers-reduced-motion the CSS swaps it for a
                static before/after (design §7.1); both are decorative. */}
            <div className="wt__ws-mock" aria-hidden="true">
              <div className="wt__ws-mock-live">
                <span className="wt__ws-mock-pane" />
                <span className="wt__ws-mock-tab" />
                <span className="wt__ws-mock-seam" />
              </div>
              <div className="wt__ws-mock-static">
                <span className="wt__ws-mock-half" />
                <span className="wt__ws-mock-half" />
              </div>
            </div>
            <p className="wt__body">
              {coarsePointer
                ? 'Hold a chat or app to pick it up — drop it at the top or bottom to split the screen.'
                : 'Drag any chat or app from the drawer to the edge of a pane to work side by side. Drop it in the middle to keep it as a tab.'}
            </p>
            {/* Teach the logo gesture: a tap still opens the menu; a HOLD (watch the
                ring) enters builder mode — plus swipe on touch, Shift+Enter on desktop. */}
            <p className="wt__body wt__body--sub">
              {coarsePointer
                ? 'Tap the mark for your chats and apps. Hold it — watch the ring — to enter builder mode, or swipe right on it.'
                : 'Tap the mark for your chats and apps. Hold it — watch the ring — to enter builder mode, or press Shift+Enter.'}
            </p>
            <div className="wt__actions">
              <button
                type="button"
                className="wt__btn wt__btn--ghost"
                onClick={skip}
              >
                Skip
              </button>
              <button
                type="button"
                className="wt__btn wt__btn--primary"
                onClick={next}
              >
                Next
              </button>
            </div>
          </>
        )}

        {step === 'safety-net' && (
          <>
            <h2 id="wt-title" className="wt__title">If something breaks</h2>
            <p className="wt__body">
              The agent can break things. That's the trade for full
              write access. Open{' '}
              <a
                href="/recover/chat"
                className="wt__link"
                target="_blank"
                rel="noopener noreferrer"
              >
                /recover/chat
              </a>
              {' '}to talk to a fresh agent with its own boot path. It
              can roll back the shell, the backend, or recent changes.
              From{' '}
              <a
                href="/recover"
                className="wt__link"
                target="_blank"
                rel="noopener noreferrer"
              >
                /recover
              </a>
              {' '}you can also back up the database or reset to factory.
              Bookmark these. They stay reachable even when the main
              chat doesn't.
            </p>
            <div className="wt__actions">
              <button
                type="button"
                className="wt__btn wt__btn--ghost"
                onClick={skip}
              >
                Skip
              </button>
              <button
                type="button"
                className="wt__btn wt__btn--primary"
                onClick={next}
              >
                Next
              </button>
            </div>
          </>
        )}

        {step === 'install' && (
          <>
            {/* Arrow-up renders BEFORE the card content so the visual
                bounce points past the card edge toward the address-bar
                menu (Chromium / Android Firefox), where the install
                action actually lives. Arrow-down renders below the
                body because Safari's Share button is at the bottom of
                the screen — the arrow should point at it, not at the
                card. */}
            {installCopy.arrowDir === 'up' && (
              <div className="wt__arrow wt__arrow--up wt__arrow--top" aria-hidden="true">↑</div>
            )}
            <h2 id="wt-title" className="wt__title">{installCopy.title}</h2>
            <p className="wt__body">{installCopy.body}</p>
            {installCopy.arrowDir === 'down' && (
              <div className="wt__arrow wt__arrow--down" aria-hidden="true">↓</div>
            )}
            {copyState === 'copied' && (
              <p className="wt__note wt__note--success">Link copied. Paste in Safari.</p>
            )}
            {copyState === 'failed' && (
              <p className="wt__note wt__note--warn">Couldn’t copy automatically. Long-press the address bar.</p>
            )}
            <div className="wt__actions">
              <button
                type="button"
                className="wt__btn wt__btn--ghost"
                onClick={skip}
              >
                Skip
              </button>
              <button
                type="button"
                className="wt__btn wt__btn--primary"
                onClick={handleInstallCta}
              >
                {installCopy.ctaLabel}
              </button>
            </div>
            {/* The CTA above advances (or copies a link) but never ends
                the walkthrough — install happens out-of-band in the
                browser's own UI, so there's no in-page event to hook.
                A user who has already installed needs an explicit way to
                clear the overlay for good; "I've installed it" finishes
                the walkthrough so it (and this fixed full-screen overlay)
                never returns. */}
            <button
              type="button"
              className="wt__skip-link"
              onClick={finish}
            >
              I’ve installed it
            </button>
          </>
        )}

        {step === 'first-chat' && (
          <>
            <h2 id="wt-title" className="wt__title">Start your first chat</h2>
            <p className="wt__body">
              Tap the chat input at the bottom and tell Möbius what
              you want to build or change. It writes the code and
              shows you the result. You can switch chats anytime
              from the drawer.
            </p>
            <div className="wt__actions">
              <button
                type="button"
                className="wt__btn wt__btn--primary wt__btn--single"
                onClick={next}
              >
                Got it
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
