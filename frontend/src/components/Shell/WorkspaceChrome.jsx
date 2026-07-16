import { useCallback, useMemo, useState } from 'react'
import { MessageSquare, AppWindow, X, Layers } from 'lucide-react'
import * as tabModel from './tabModel.js'
import {
  projectLayout, STRIP_H, WORKSPACE_SPLITS_ENABLED,
} from './paneModel.js'
import { ARROW_STEP_RATIO } from '../../lib/splitHelper.js'

// The chrome layer for a tiled (≥2 visible leaves) workspace (design §2). It is
// a sibling AFTER the flat content wrappers, absolute inset:0, pointer-events
// none except its own children, and carries its OWN `inert` (Shell passes it).
// Nothing here reparents content — panes are rectangles the content wrappers are
// positioned into; this layer only draws the strips, focus ring, dividers, and
// the phone overflow chip/sheet over them.
//
// Divider drag is imperative and React-free per frame (design §2): pointerdown
// caches the affected wrapper/strip/divider elements, each move re-projects with
// a ratio override and writes their rects directly, pointerup commits SET_RATIO.
// No reducer dispatch happens per move.

const HIT = 44 // divider hit target (the visible hairline is 1px inside it)

function cssEsc(v) {
  return (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape(String(v)) : String(v)
}

function setRect(el, rect) {
  if (!el) return
  el.style.left = `${rect.x}px`
  el.style.top = `${rect.y}px`
  el.style.width = `${rect.w}px`
  el.style.height = `${rect.h}px`
}

// A pane's CONTENT rect is its pane rect minus the strip row on top.
function contentRectOfPane(paneRect) {
  return {
    x: paneRect.x,
    y: paneRect.y + STRIP_H,
    w: paneRect.w,
    h: Math.max(0, paneRect.h - STRIP_H),
  }
}

// The 44px hit rectangle around a divider's thin visible gap.
function dividerHitRect(d) {
  if (d.dir === 'row') {
    return { x: Math.round(d.x + d.w / 2 - HIT / 2), y: d.y, w: HIT, h: d.h }
  }
  return { x: d.x, y: Math.round(d.y + d.h / 2 - HIT / 2), w: d.w, h: HIT }
}

function PaneStrip({
  pane, paneRect, focused, labelForTab,
  onActivate, onClose, onFocus, onTabContextMenu,
}) {
  return (
    <div
      className={`workspace__strip shell__tabstrip${focused ? ' workspace__strip--focused' : ''}`}
      data-pane-strip={pane.id}
      role="tablist"
      aria-label="Pane tabs"
      style={{ left: paneRect.x, top: paneRect.y, width: paneRect.w, height: STRIP_H }}
      onPointerDown={() => onFocus(pane.id)}
    >
      {pane.tabs.map(tab => {
        const isChat = tab.kind === 'chat'
        const key = tabModel.tabKey(tab)
        const active = key === pane.activeTabKey
        const TabIcon = isChat ? MessageSquare : AppWindow
        const label = labelForTab(tab)
        return (
          <div
            key={key}
            className={`shell__tab${active ? ' shell__tab--active' : ''}`}
          >
            <button
              type="button"
              className="shell__tab-open"
              role="tab"
              aria-selected={active ? 'true' : 'false'}
              onClick={() => onActivate(pane.id, tab)}
              onContextMenu={(e) => onTabContextMenu(e, tab, pane.id)}
            >
              <TabIcon size={13} aria-hidden="true" />
              <span className="shell__tab-text">{label}</span>
            </button>
            <button
              type="button"
              className="shell__tab-close"
              aria-label={`Close ${label} tab`}
              onClick={() => onClose(tab)}
            >
              <X size={13} aria-hidden="true" />
            </button>
          </div>
        )
      })}
    </div>
  )
}

function Divider({ divider, onPointerDown, onKeyDown, onDoubleClick }) {
  const hit = dividerHitRect(divider)
  const vertical = divider.dir === 'row'
  return (
    <div
      className={`workspace__divider workspace__divider--${vertical ? 'v' : 'h'}`}
      data-divider={divider.splitId}
      role="separator"
      tabIndex={0}
      aria-orientation={vertical ? 'vertical' : 'horizontal'}
      aria-valuenow={Math.round(divider.ratio * 100)}
      aria-valuemin={10}
      aria-valuemax={90}
      aria-label="Resize panes"
      style={{ left: hit.x, top: hit.y, width: hit.w, height: hit.h }}
      onPointerDown={(e) => onPointerDown(e, divider)}
      onKeyDown={(e) => onKeyDown(e, divider)}
      onDoubleClick={() => onDoubleClick(divider)}
    >
      <span className="workspace__divider-bar" aria-hidden="true" />
    </div>
  )
}

export default function WorkspaceChrome({
  inert = false,
  workspace,
  projection,
  mode,
  contentRect,
  contentElRef,
  dispatchWorkspace,
  navTo,
  labelForTab,
  onTabContextMenu,
}) {
  const [sheetOpen, setSheetOpen] = useState(false)

  const focusPane = useCallback((paneId) => {
    dispatchWorkspace({ type: 'FOCUS', paneId })
  }, [dispatchWorkspace])

  // Tab activation in a pane routes entirely through navTo (design §1): navTo's
  // one OPEN_TAB into `paneId` activates the tab AND focuses the pane, and its
  // back-target snapshot is the pane we're leaving. Pre-dispatching SET_ACTIVE/
  // FOCUS here would advance the workspace ref before navTo snapshots it, making
  // the just-activated tab its own (wrong) back-target.
  const activateTab = useCallback((paneId, tab) => {
    const { view, opts } = tabModel.tabNavTarget(tab)
    navTo(view, { ...opts, paneId })
  }, [navTo])

  const closeTab = useCallback((tab) => {
    dispatchWorkspace({ type: 'CLOSE_TAB', tabKey: tabModel.tabKey(tab) })
  }, [dispatchWorkspace])

  // ── Divider drag (imperative, React-free per frame) ──────────────────────
  const beginDrag = useCallback((e, divider) => {
    if (e.button != null && e.button !== 0) return
    const contentEl = contentElRef.current
    if (!contentEl) return
    e.preventDefault()
    const handle = e.currentTarget
    try { handle.setPointerCapture(e.pointerId) } catch { /* not captured */ }
    const prevUserSelect = document.body.style.userSelect
    document.body.style.userSelect = 'none'
    const box = contentEl.getBoundingClientRect()
    const { dir, splitId } = divider

    // Cache the elements to move; no React render fires during the drag, so the
    // DOM is stable and one lookup suffices.
    const paneEls = new Map()
    for (const paneId of projection.visibleLeaves) {
      const pane = workspace.panes[paneId]
      const activeKey = pane?.activeTabKey
      paneEls.set(paneId, {
        wrapper: activeKey
          ? contentEl.querySelector(`[data-tab-key="${cssEsc(activeKey)}"]`)
          : null,
        strip: contentEl.querySelector(`[data-pane-strip="${cssEsc(paneId)}"]`),
      })
    }
    const dividerEls = new Map()
    for (const d of projection.dividers) {
      dividerEls.set(d.splitId, contentEl.querySelector(`[data-divider="${cssEsc(d.splitId)}"]`))
    }
    const focusRingEl = contentEl.querySelector('[data-focus-ring]')

    let committed = divider.ratio

    const paint = (clientX, clientY) => {
      const axis = dir === 'row' ? (clientX - box.left) : (clientY - box.top)
      const raw = divider.span > 0 ? (axis - divider.origin) / divider.span : 0.5
      const proj = projectLayout(workspace, mode, contentRect, { splitId, ratio: raw })
      committed = proj.dividers.find(d => d.splitId === splitId)?.ratio ?? committed
      for (const paneId of proj.visibleLeaves) {
        const rect = proj.rects[paneId]
        const els = paneEls.get(paneId)
        if (!rect || !els) continue
        if (els.wrapper) setRect(els.wrapper, contentRectOfPane(rect))
        if (els.strip) setRect(els.strip, { x: rect.x, y: rect.y, w: rect.w, h: STRIP_H })
      }
      for (const d of proj.dividers) {
        const el = dividerEls.get(d.splitId)
        if (!el) continue
        setRect(el, dividerHitRect(d))
        el.setAttribute('aria-valuenow', String(Math.round(d.ratio * 100)))
      }
      const fr = proj.rects[workspace.focusedPaneId]
      if (focusRingEl && fr) setRect(focusRingEl, fr)
    }

    const onMove = (ev) => paint(ev.clientX, ev.clientY)
    const onUp = (ev) => {
      handle.removeEventListener('pointermove', onMove)
      handle.removeEventListener('pointerup', onUp)
      handle.removeEventListener('pointercancel', onUp)
      try { handle.releasePointerCapture(ev.pointerId) } catch { /* released */ }
      document.body.style.userSelect = prevUserSelect
      dispatchWorkspace({ type: 'SET_RATIO', splitId, ratio: committed })
    }
    handle.addEventListener('pointermove', onMove)
    handle.addEventListener('pointerup', onUp)
    handle.addEventListener('pointercancel', onUp)
  }, [contentElRef, dispatchWorkspace, mode, contentRect, projection, workspace])

  const onDividerKeyDown = useCallback((e, divider) => {
    const step = e.shiftKey ? 0.10 : ARROW_STEP_RATIO
    const grow = divider.dir === 'row'
      ? { ArrowRight: step, ArrowLeft: -step }
      : { ArrowDown: step, ArrowUp: -step }
    let ratio = divider.ratio
    if (e.key in grow) ratio += grow[e.key]
    else if (e.key === 'Home') ratio = 0.1
    else if (e.key === 'End') ratio = 0.9
    else if (e.key === 'Enter') ratio = 0.5
    else return
    e.preventDefault()
    dispatchWorkspace({ type: 'SET_RATIO', splitId: divider.splitId, ratio })
  }, [dispatchWorkspace])

  const resetDivider = useCallback((divider) => {
    dispatchWorkspace({ type: 'SET_RATIO', splitId: divider.splitId, ratio: 0.5 })
  }, [dispatchWorkspace])

  // ── Phone/compact overflow: the pane chip + bottom sheet (design §4) ──────
  const allLeaves = useMemo(
    () => Object.keys(workspace.panes),
    [workspace.panes],
  )
  const hasOverflow = allLeaves.length > projection.visibleLeaves.length
  const showChip = WORKSPACE_SPLITS_ENABLED
    && (mode === 'phone' || mode === 'compact')
    && hasOverflow

  const pickPane = useCallback((paneId) => {
    setSheetOpen(false)
    const pane = workspace.panes[paneId]
    const active = pane?.tabs.find(t => tabModel.tabKey(t) === pane.activeTabKey)
    if (active) {
      const { view, opts } = tabModel.tabNavTarget(active)
      navTo(view, { ...opts, paneId })
    } else {
      dispatchWorkspace({ type: 'FOCUS', paneId })
    }
  }, [dispatchWorkspace, navTo, workspace.panes])

  const focusRect = projection.rects[workspace.focusedPaneId]
  const chipHostRect = focusRect || projection.rects[projection.visibleLeaves[0]]

  return (
    <div className="workspace__chrome" data-workspace-chrome inert={inert || undefined}>
      {projection.visibleLeaves.map(paneId => {
        const pane = workspace.panes[paneId]
        const rect = projection.rects[paneId]
        if (!pane || !rect) return null
        return (
          <PaneStrip
            key={paneId}
            pane={pane}
            paneRect={rect}
            focused={paneId === workspace.focusedPaneId}
            labelForTab={labelForTab}
            onActivate={activateTab}
            onClose={closeTab}
            onFocus={focusPane}
            onTabContextMenu={onTabContextMenu}
          />
        )
      })}

      {focusRect && (
        <div
          className="workspace__focus-ring"
          data-focus-ring
          aria-hidden="true"
          style={{ left: focusRect.x, top: focusRect.y, width: focusRect.w, height: focusRect.h }}
        />
      )}

      {projection.dividers.map(divider => (
        <Divider
          key={divider.splitId}
          divider={divider}
          onPointerDown={beginDrag}
          onKeyDown={onDividerKeyDown}
          onDoubleClick={resetDivider}
        />
      ))}

      {showChip && chipHostRect && (
        <button
          type="button"
          className="workspace__pane-chip"
          style={{ left: chipHostRect.x + chipHostRect.w - 60, top: chipHostRect.y + 5 }}
        aria-haspopup="dialog"
        aria-expanded={sheetOpen}
        aria-label={`Show panes, ${projection.visibleLeaves.length} of ${allLeaves.length} visible`}
          onClick={() => setSheetOpen(true)}
        >
          <Layers size={13} aria-hidden="true" />
          <span>{projection.visibleLeaves.length}/{allLeaves.length}</span>
        </button>
      )}

      {showChip && sheetOpen && (
        <div className="workspace__sheet-scrim" onPointerDown={() => setSheetOpen(false)}>
          <div
            className="workspace__sheet"
            role="dialog"
            aria-label="Switch pane"
            onPointerDown={(e) => e.stopPropagation()}
          >
            <div className="workspace__sheet-title">Panes</div>
            {allLeaves.map(paneId => {
              const pane = workspace.panes[paneId]
              const active = pane?.tabs.find(t => tabModel.tabKey(t) === pane.activeTabKey)
              const label = active ? labelForTab(active) : 'Empty'
              const visible = projection.visibleLeaves.includes(paneId)
              return (
                <button
                  key={paneId}
                  type="button"
                  className={`workspace__sheet-row${visible ? ' workspace__sheet-row--visible' : ''}`}
                  onClick={() => pickPane(paneId)}
                >
                  <span className="workspace__sheet-row-title">{label}</span>
                  <span className="workspace__sheet-row-count">{pane?.tabs.length || 0}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
