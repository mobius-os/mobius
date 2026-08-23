import { useLayoutEffect, useRef } from 'react'
import { CollapseSm, ExpandSm, X } from '@openai/apps-sdk-ui/components/Icon'
import * as tabModel from './tabModel.js'
import { STRIP_H } from './paneModel.js'
import { captureLayoutSpace, clientLengthToLayout } from '../../lib/layoutSpace.js'

// Each direction owns 40% of the one-shot cycle, so 1000/14.4 ms per clipped pixel
// keeps travel at a readable 36px/s. The cycle runs once, returns to the beginning,
// and stops; duration therefore scales with distance instead of making long manual
// titles accelerate.
const TITLE_CYCLE_MS_PER_PX = 1000 / 14.4

function paneDomToken(value) {
  return encodeURIComponent(String(value))
}

export function paneTabDomId(paneId, tabKey) {
  return `pane-tab-${paneDomToken(paneId)}-${paneDomToken(tabKey)}`
}

export function panePanelDomId(paneId, tabKey) {
  return `pane-panel-${paneDomToken(paneId)}-${paneDomToken(tabKey)}`
}

// The ONE strip implementation, shared by the multi-pane chrome overlay AND the
// single-pane top nav (design §2/§3.6). The two CONTAINERS differ by a scroll
// constraint — an absolute chrome strip vs the flow <nav> — but the .shell__tab
// trio, the roving-tabindex keyboard model, and active-ness (always derived from
// the workspace's own active tab, never a legacy nav triple) are identical, so
// they live here once instead of being hand-rolled twice.

// Roving-tabindex keyboard navigation: a strip is one tab stop; Left/Right wrap,
// Home/End jump to the ends, and Delete/Backspace closes the focused tab after
// moving focus to its neighbour. Up/Down stay native in this horizontal widget.
// `tabs` is the strip's tab list in render order (the i-th `.shell__tab-open`
// button is the i-th tab); `onClose` closes one.
export function stripKeyDown(e, tabs, onClose) {
  const buttons = [...e.currentTarget.querySelectorAll('.shell__tab-open')]
  const i = buttons.indexOf(document.activeElement)
  if (i === -1) return
  let next = -1
  if (e.key === 'ArrowRight') next = (i + 1) % buttons.length
  else if (e.key === 'ArrowLeft') next = (i - 1 + buttons.length) % buttons.length
  else if (e.key === 'Home') next = 0
  else if (e.key === 'End') next = buttons.length - 1
  else if (e.key === 'Delete' || e.key === 'Backspace') {
    e.preventDefault()
    if (tabs[i]) {
      const neighbour = buttons[i + 1] || buttons[i - 1]
      if (neighbour) neighbour.focus()
      else document.querySelector('.shell__brand')?.focus()
      onClose(tabs[i])
    }
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
  if (e.deltaMode === 0) {
    strip.scrollLeft += clientLengthToLayout(e.deltaY, captureLayoutSpace(strip))
    return
  }
  const scale = e.deltaMode === 1 ? 16 : strip.clientWidth
  strip.scrollLeft += e.deltaY * scale
}

// The presentational tab button (open + close). `role="tab"` inside the tablist
// chrome strip; the flow nav omits it (a nav landmark, not a tablist) and marks
// the current tab with aria-current instead. Only the active tab is tabbable
// (tabIndex 0); the rest and every close button are reached via stripKeyDown.
export function PaneTab({
  tab, label, active, focused = true, revealKey = 0,
  tabIndex, dragKey, role, tabId, controlsId,
  onActivate, onClose, onContextMenu,
}) {
  const tabRef = useRef(null)
  const titleRef = useRef(null)
  // Only the active CHAT title cycles, and only when it is actually clipped. One
  // ResizeObserver follows that one focused title; measurements update CSS vars
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
        const duration = Math.round(shift * TITLE_CYCLE_MS_PER_PX)
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

  return (
    <div ref={tabRef} className={`shell__tab${active ? ' shell__tab--active' : ''}`}>
      <button
        type="button"
        className="shell__tab-open"
        role={role}
        id={tabId}
        aria-controls={role === 'tab' ? controlsId : undefined}
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
        <X width={13} height={13} aria-hidden="true" />
      </button>
    </div>
  )
}

export function PaneFocusButton({ paneId, focused, onToggle }) {
  const label = focused ? 'Show all panes' : 'Focus pane'
  const Icon = focused ? CollapseSm : ExpandSm
  return (
    <button
      type="button"
      className="workspace__pane-focus"
      aria-label={label}
      title={label}
      onClick={() => onToggle(paneId)}
    >
      <Icon width={14} height={14} aria-hidden="true" />
    </button>
  )
}

// The absolute per-pane strip in the multi-pane chrome overlay. The strip focuses
// its pane on WHITESPACE pointerdown only — a tab focuses via navTo, and
// pre-focusing on the tab's own pointerdown would advance the workspace ref
// before navTo snapshots the source route (see WorkspaceChrome.activateTab).
export function PaneStrip({
  pane, paneRect, focused, labelForTab,
  onActivate, onClose, onFocus, onTabContextMenu,
  viewTransitionStyle = null,
  canFocusPane = false, paneFocused = false, onTogglePaneFocus,
  revealKey = 0,
}) {
  const style = {
    left: paneRect.x,
    top: paneRect.y,
    width: paneRect.w,
    height: STRIP_H,
    ...(viewTransitionStyle || {}),
  }
  return (
    <div
      className={`workspace__strip shell__tabstrip${focused ? ' workspace__strip--focused' : ''}`}
      data-pane-strip={pane.id}
      data-mode-pane-vt={viewTransitionStyle ? pane.id : undefined}
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
            tabId={paneTabDomId(pane.id, key)}
            controlsId={panePanelDomId(pane.id, key)}
            tabIndex={active ? 0 : -1}
            dragKey={key}
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
