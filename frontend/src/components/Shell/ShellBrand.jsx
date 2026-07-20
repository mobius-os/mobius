import { memo, useRef } from 'react'
import { BASE } from '../../api/client.js'
import { useLogoModeGesture } from './useLogoModeGesture.js'
import useLivingHalo from './useLivingHalo.js'

/**
 * The brand owns its transient press/hold animation state. Keeping that state in
 * this memoized leaf prevents a pointerdown/up on the navigation toggle from
 * rerendering Shell and every row in a large drawer.
 */
const ShellBrand = memo(function ShellBrand({
  brandRef,
  splitsEnabled,
  navigationOpen,
  builderModeActive,
  backFiredRef,
  onToggleMode,
  onToggleNavigation,
}) {
  const keyboardModeClickRef = useRef(false)
  const haloRef = useRef(null)
  const logoGesture = useLogoModeGesture({
    onToggleMode,
    brandRef,
    enabled: splitsEnabled,
    // Cancel a live hold if navigation opens by any other path.
    drawerOpen: navigationOpen,
    builderModeActive,
  })
  useLivingHalo({ haloRef, active: splitsEnabled && builderModeActive })

  return (
    <>
      <button
        ref={brandRef}
        type="button"
        className={`shell__brand${logoGesture.holding ? ' is-holding' : ''}`
          + `${logoGesture.flourish ? ` is-${logoGesture.flourish}` : ''}`
          + `${builderModeActive ? ' shell__brand--builder' : ''}`}
        // Navigation remains the primary, stable accessible name. The builder
        // gesture is supplementary and its state is announced below.
        aria-label="Toggle navigation"
        aria-description={splitsEnabled
          ? 'Hold or press Shift+Enter for builder mode'
          : undefined}
        aria-controls="navigation-drawer"
        aria-expanded={navigationOpen}
        onPointerDown={(e) => {
          // A deliberate interaction immediately clears Android's compatibility-
          // click guard left by an OS Back gesture.
          backFiredRef.current = false
          if (splitsEnabled) logoGesture.onPointerDown(e)
        }}
        onPointerMove={splitsEnabled ? logoGesture.onPointerMove : undefined}
        onPointerUp={splitsEnabled ? logoGesture.onPointerUp : undefined}
        onPointerCancel={splitsEnabled ? logoGesture.onPointerCancel : undefined}
        onContextMenu={splitsEnabled ? logoGesture.onContextMenu : undefined}
        onLostPointerCapture={splitsEnabled ? logoGesture.onLostPointerCapture : undefined}
        onKeyDown={(e) => {
          backFiredRef.current = false
          // A keyboard interaction clears pointer provenance so a keyboard-invoked
          // contextmenu on the focused brand reaches the native menu instead of
          // inheriting a stale touch/pen suppression.
          if (splitsEnabled) logoGesture.onKeyDown()
          // e.repeat guard: holding Shift+Enter must fire ONE toggle, not a storm
          // of them at the keyboard repeat rate (INV 3).
          if (splitsEnabled && e.shiftKey && e.key === 'Enter' && !e.repeat) {
            e.preventDefault()
            keyboardModeClickRef.current = true
            onToggleMode()
          }
        }}
        onKeyUp={(e) => {
          // Tie the synthesized-click suppression to THIS key activation: a
          // prevented Shift+Enter usually produces no compatibility click, so the
          // flag would otherwise leak and swallow the NEXT plain Enter/Space click
          // (finding 12). Clearing on keyup bounds it to the one activation; a real
          // Enter fires its click on keydown (before this), so nothing is lost.
          if (e.key === 'Enter') keyboardModeClickRef.current = false
        }}
        onClick={(e) => {
          if (backFiredRef.current) return
          if (keyboardModeClickRef.current && e.detail === 0) {
            keyboardModeClickRef.current = false
            return
          }
          keyboardModeClickRef.current = false
          // A hold/swipe/drag consumes only its trailing pointer click. Keyboard
          // activation (detail 0) always retains the navigation action.
          if (logoGesture.consumeSuppressedClick(e.detail)) return
          onToggleNavigation()
        }}
        onAnimationEnd={logoGesture.onAnimationEnd}
      >
        <span className="shell__logo-wrap">
          {splitsEnabled && (
            <span ref={haloRef} className="shell__logo-halo" aria-hidden="true" />
          )}
          {/* Decorative and pointer-inert: the button owns long presses, so mobile
              browsers cannot raise a native image preview over the gesture. */}
          <img
            className="shell__logo"
            src={`${BASE}/moebius.png`}
            alt=""
            width="30"
            height="30"
            draggable={false}
          />
        </span>
        <span className="shell__wordmark">Möbius</span>
      </button>
      {splitsEnabled && (
        <span className="shell__sr-only" role="status" aria-live="polite">
          {builderModeActive ? 'Builder mode' : 'Single screen'}
        </span>
      )}
    </>
  )
})

export default ShellBrand
