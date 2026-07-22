import { useLayoutEffect, useRef } from 'react'
import AppWindow from 'lucide-react/dist/esm/icons/app-window.mjs'
import Maximize2 from 'lucide-react/dist/esm/icons/maximize-2.mjs'
import MessageSquare from 'lucide-react/dist/esm/icons/message-square.mjs'
import Minimize2 from 'lucide-react/dist/esm/icons/minimize-2.mjs'
import Settings from 'lucide-react/dist/esm/icons/settings.mjs'
import X from 'lucide-react/dist/esm/icons/x.mjs'
import * as tabModel from './tabModel.js'
import { STRIP_H, WORKSPACE_SPLITS_ENABLED } from './paneModel.js'

// Keep normal generated chat titles near a steady, readable 30px/s while the
// existing one-shot cycle traverses each direction. The floor keeps small clips
// unhurried; the cap prevents an extreme manual 500-character rename from
// holding a compositor animation for minutes.
const TITLE_CYCLE_MIN_MS = 8000
const TITLE_CYCLE_MAX_MS = 32000
const TITLE_CYCLE_MS_PER_PX = 1000 / 6

// The ONE strip implementation, shared by the multi-pane chrome overlay AND the
// single-pane top nav (design §2/§3.6). The two CONTAINERS differ by a scroll
// constraint — an absolute chrome strip vs the flow <nav> — but the .shell__tab
// trio, the roving-tabindex keyboard model, and active-ness (always derived from
// the workspace's own active tab, never a legacy nav triple) are identical, so
// they live here once instead of being hand-rolled twice.

// Roving-tabindex keyboard navigation (WAI-ARIA toolbar): a strip is one tab
// stop, and arrows move focus between its tab buttons, Home/End jump to the ends,
// Delete/Backspace closes the focused tab. `tabs` is the strip's tab list in
// render order (the i-th `.shell__tab-open` button is the i-th tab); `onClose`
// closes one. Works for either container because it keys on the shared class, not
// a role.
export function stripKeyDown(e, tabs, onClose) {
  const buttons = [...e.currentTarget.querySelectorAll('.shell__tab-open')]
  const i = buttons.indexOf(document.activeElement)
  if (i === -1) return
  let next = -1
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = Math.min(i + 1, buttons.length - 1)
  else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = Math.max(i - 1, 0)
  else if (e.key === 'Home') next = 0
  else if (e.key === 'End') next = buttons.length - 1
  else if (e.key === 'Delete' || e.key === 'Backspace') {
    e.preventDefault()
    if (tabs[i]) onClose(tabs[i])
    return
  } else return
  e.preventDefault()
  buttons[next]?.focus()
}

// A trackpad already sends horizontal deltaX and remains fully native. Translate
// only a dominant vertical wheel into the hidden horizontal overflow so a mouse
// wheel can reach every tab without adding another control or persistent chrome.
export function scrollStripWheel(e) {
  if (Math.abs(e.deltaX) >= Math.abs(e.deltaY) || e.deltaY === 0) return
  const strip = e.currentTarget
  if (strip.scrollWidth <= strip.clientWidth) return
  const scale = e.deltaMode === 1 ? 16 : (e.deltaMode === 2 ? strip.clientWidth : 1)
  strip.scrollLeft += e.deltaY * scale
}

// The presentational tab button (open + close). `role="tab"` inside the tablist
// chrome strip; the flow nav omits it (a nav landmark, not a tablist) and marks
// the current tab with aria-current instead. Only the active tab is tabbable
// (tabIndex 0); the rest and every close button are reached via stripKeyDown.
export function PaneTab({
  tab, label, active, focused = true, revealKey = 0,
  tabIndex, dragKey, role, onActivate, onClose, onContextMenu,
}) {
  const tabRef = useRef(null)
  const titleRef = useRef(null)
  // Only the active CHAT title cycles, and only when it is actually clipped. One
  // ResizeObserver follows that one title per pane; measurements update CSS vars
  // imperatively, so neither resizing nor the animation causes React renders.
  useLayoutEffect(() => {
    const title = titleRef.current
    if (!title) return undefined
    const text = title.firstElementChild
    const clear = () => {
      delete title.dataset.overflow
      title.style.removeProperty('--tab-title-shift')
      title.style.removeProperty('--tab-title-duration')
    }
    if (!active || !focused || tab.kind !== 'chat' || !text) {
      clear()
      return undefined
    }
    const measure = () => {
      const shift = Math.ceil(text.scrollWidth - title.clientWidth)
      if (shift > 3) {
        const duration = Math.min(
          TITLE_CYCLE_MAX_MS,
          Math.max(TITLE_CYCLE_MIN_MS, Math.round(shift * TITLE_CYCLE_MS_PER_PX)),
        )
        title.dataset.overflow = 'true'
        title.style.setProperty('--tab-title-shift', `-${shift}px`)
        title.style.setProperty('--tab-title-duration', `${duration}ms`)
      } else {
        clear()
      }
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return clear
    const observer = new ResizeObserver(measure)
    observer.observe(title)
    observer.observe(text)
    return () => {
      observer.disconnect()
      clear()
    }
  }, [active, focused, label, tab.kind])

  // A tab activated from outside the strip (drawer/history restore) must not stay
  // clipped beyond an overflow edge. Browser focus already handles keyboard
  // navigation; this covers state-driven activation without a React state loop.
  useLayoutEffect(() => {
    if (active && focused) {
      tabRef.current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
    }
  }, [active, focused, revealKey])

  const TabIcon = tab.kind === 'settings'
    ? Settings
    : (tab.kind === 'chat' ? MessageSquare : AppWindow)
  return (
    <div ref={tabRef} className={`shell__tab${active ? ' shell__tab--active' : ''}`}>
      <button
        type="button"
        className="shell__tab-open"
        role={role}
        aria-selected={role === 'tab' ? (active ? 'true' : 'false') : undefined}
        aria-current={role !== 'tab' && active ? 'true' : undefined}
        tabIndex={tabIndex}
        title={label}
        // The drag controller picks tab sources up by this attribute; only present
        // when the splits flag is on so a flag-off build carries no drag hooks.
        data-drag-key={dragKey}
        onClick={onActivate}
        // Middle-click closes the tab (standard browser-tab convention), routed
        // through the SAME onClose the ✕ button uses — identical semantics (undo
        // slot, history retargeting); no parallel close path. auxclick is the
        // standard middle-activation event; the mousedown preventDefault stops
        // the platform autoscroll circle from appearing on the press. Web/desktop
        // only — middle-click has no touch equivalent, so there is nothing to gate.
        // A middle press cannot arm a drag: useWorkspaceDrag's onPointerDown bails
        // on any non-primary mouse button before it reads data-drag-key.
        onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); onClose() } }}
        onMouseDown={(e) => { if (e.button === 1) e.preventDefault() }}
        onContextMenu={onContextMenu}
      >
        {/* Reuse the tab's existing kind icon as the touch reorder region. The
            transparent padding enlarges its hit box without adding visible chrome
            or consuming any more tab width. The rest of the tab remains native
            pan-x so an overflowing strip can still be scrolled. */}
        <span
          className="shell__tab-kind"
          data-touch-drag-handle={dragKey}
          aria-hidden="true"
        >
          <TabIcon size={13} />
        </span>
        <span ref={titleRef} className="shell__tab-text">
          <span className="shell__tab-text-inner">{label}</span>
        </span>
      </button>
      <button
        type="button"
        className="shell__tab-close"
        aria-label={`Close ${label} tab`}
        tabIndex={-1}
        onClick={onClose}
      >
        <X size={13} aria-hidden="true" />
      </button>
    </div>
  )
}

export function PaneFocusButton({ paneId, focused, onToggle }) {
  const label = focused ? 'Show all panes' : 'Focus pane'
  const Icon = focused ? Minimize2 : Maximize2
  return (
    <button
      type="button"
      className="workspace__pane-focus"
      aria-label={label}
      aria-pressed={focused ? 'true' : 'false'}
      title={label}
      onClick={() => onToggle(paneId)}
    >
      <Icon size={14} aria-hidden="true" />
    </button>
  )
}

// The absolute per-pane strip in the multi-pane chrome overlay. The strip focuses
// its pane on WHITESPACE pointerdown only — a tab focuses via navTo, and
// pre-focusing on the tab's own pointerdown would advance the workspace ref
// before navTo snapshots the source route (see WorkspaceChrome.activateTab).
export function PaneStrip({
  pane, paneRect, focused, labelForTab,
  onActivate, onClose, onFocus, onTabContextMenu, motion = null,
  canFocusPane = false, paneFocused = false, onTogglePaneFocus,
  revealKey = 0,
}) {
  // The strip deals WITH its pane during a mode beat (exit-design v2): the same
  // compositor-only data-mode-motion + --mode-duration/--mode-delay the pane wrapper
  // carries, merged into the strip's absolute-position style. A promote (survivor)
  // strip clears upward; a deal-out/deal-in strip moves with its pane.
  const style = motion
    ? { left: paneRect.x, top: paneRect.y, width: paneRect.w, height: STRIP_H, ...motion.vars }
    : { left: paneRect.x, top: paneRect.y, width: paneRect.w, height: STRIP_H }
  return (
    <div
      className={`workspace__strip shell__tabstrip${focused ? ' workspace__strip--focused' : ''}`}
      data-pane-strip={pane.id}
      data-mode-motion={motion ? motion.motion : undefined}
      role="tablist"
      aria-label="Pane tabs"
      style={style}
      onKeyDown={(e) => stripKeyDown(e, pane.tabs, onClose)}
      onWheel={scrollStripWheel}
      onPointerDown={(e) => {
        if (!e.target.closest('.shell__tab, .workspace__pane-focus')) onFocus(pane.id)
      }}
    >
      {pane.tabs.map((tab) => {
        const key = tabModel.tabKey(tab)
        const active = key === pane.activeTabKey
        return (
          <PaneTab
            key={key}
            tab={tab}
            label={labelForTab(tab)}
            active={active}
            focused={focused}
            revealKey={revealKey}
            role="tab"
            tabIndex={active ? 0 : -1}
            dragKey={WORKSPACE_SPLITS_ENABLED ? key : undefined}
            onActivate={() => onActivate(pane.id, tab)}
            onClose={() => onClose(tab)}
            onContextMenu={(e) => onTabContextMenu(e, tab, pane.id)}
          />
        )
      })}
      {canFocusPane && onTogglePaneFocus && (
        <PaneFocusButton
          paneId={pane.id}
          focused={paneFocused}
          onToggle={onTogglePaneFocus}
        />
      )}
    </div>
  )
}
