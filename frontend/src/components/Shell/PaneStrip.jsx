import AppWindow from 'lucide-react/dist/esm/icons/app-window.mjs'
import MessageSquare from 'lucide-react/dist/esm/icons/message-square.mjs'
import Settings from 'lucide-react/dist/esm/icons/settings.mjs'
import X from 'lucide-react/dist/esm/icons/x.mjs'
import * as tabModel from './tabModel.js'
import { STRIP_H, WORKSPACE_SPLITS_ENABLED } from './paneModel.js'

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

// The presentational tab button (open + close). `role="tab"` inside the tablist
// chrome strip; the flow nav omits it (a nav landmark, not a tablist) and marks
// the current tab with aria-current instead. Only the active tab is tabbable
// (tabIndex 0); the rest and every close button are reached via stripKeyDown.
export function PaneTab({
  tab, label, active, tabIndex, dragKey, role, onActivate, onClose, onContextMenu,
}) {
  const TabIcon = tab.kind === 'settings'
    ? Settings
    : (tab.kind === 'chat' ? MessageSquare : AppWindow)
  return (
    <div className={`shell__tab${active ? ' shell__tab--active' : ''}`}>
      <button
        type="button"
        className="shell__tab-open"
        role={role}
        aria-selected={role === 'tab' ? (active ? 'true' : 'false') : undefined}
        aria-current={role !== 'tab' && active ? 'true' : undefined}
        tabIndex={tabIndex}
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
        <TabIcon size={13} aria-hidden="true" />
        <span className="shell__tab-text">{label}</span>
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

// The absolute per-pane strip in the multi-pane chrome overlay. The strip focuses
// its pane on WHITESPACE pointerdown only — a tab focuses via navTo, and
// pre-focusing on the tab's own pointerdown would advance the workspace ref
// before navTo snapshots the source route (see WorkspaceChrome.activateTab).
export function PaneStrip({
  pane, paneRect, focused, labelForTab,
  onActivate, onClose, onFocus, onTabContextMenu, motion = null,
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
      onPointerDown={(e) => { if (!e.target.closest('.shell__tab')) onFocus(pane.id) }}
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
            role="tab"
            tabIndex={active ? 0 : -1}
            dragKey={WORKSPACE_SPLITS_ENABLED ? key : undefined}
            onActivate={() => onActivate(pane.id, tab)}
            onClose={() => onClose(tab)}
            onContextMenu={(e) => onTabContextMenu(e, tab, pane.id)}
          />
        )
      })}
    </div>
  )
}
