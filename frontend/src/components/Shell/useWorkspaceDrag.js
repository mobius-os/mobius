import { useEffect } from 'react'
import * as tabModel from './tabModel.js'
import {
  buildScene, hitTest, zoneTarget, chipOffset,
  passedSlop, preHoldMoveCancels, releasedInPlace, holdMsFor, crossedDrawerExit,
} from './dragController.js'

// The thin React binding for the workspace drag controller (design §3). It owns
// only the side-effects the pure dragController.js cannot: pointer capture, the
// hold/slop timers, the chip/preview/shield/pre-glow DOM, the drawer stand-down,
// and the ONE reducer dispatch on drop. Every geometric decision is delegated to
// the pure module, so this file has no thresholds or zone math of its own.
//
// Architecture (design §3.1): a single capture-phase `pointerdown` on the
// document identifies a drag source by its `data-drag-key` (strip tabs in the
// chrome, rows in the drawer) and geometrically hit-tests against projectLayout
// rects — never DOM-event bubbling, because iframes swallow events and the
// drawer is inert. Everything is gated behind `enabled` (WORKSPACE_SPLITS_ENABLED),
// so with the flag off the listener never installs and the shell is unchanged.

// Split a tab key ("chat:5" / "app:42") back into a tabModel tab.
function tabFromKey(key) {
  const i = key.indexOf(':')
  if (i < 0) return null
  return tabModel.makeTab(key.slice(0, i), key.slice(i + 1))
}

export default function useWorkspaceDrag({
  enabled,
  contentElRef,
  sceneInputsRef, // ref → { projection, mode, contentRect }
  workspaceStateRef, // ref → { ws, undo } (advanced synchronously by Shell's dispatch)
  dispatchWorkspace,
  showUndoToast, // (label) => void
  labelForTabRef, // ref → (tab) => string
  dragActiveRef, // shared flag the Drawer's swipe-close handlers stand down on
  drawerOpenRef,
  closeDrawer,
  openTabMenuAtRef, // ref → (clientX, clientY, tab, paneId) => void
  onDragStart, // dismiss the coachmark on the first real drag
}) {
  useEffect(() => {
    if (!enabled) return undefined

    // ── Reusable overlay DOM (created lazily on the first arm) ────────────────
    let shieldEl = null
    let chipEl = null
    let previewEl = null

    function contentBox() {
      return contentElRef.current?.getBoundingClientRect() || { left: 0, top: 0 }
    }
    // Viewport client coords → content-local coords (projectLayout's space).
    function toLocal(clientX, clientY) {
      const box = contentBox()
      return { x: clientX - box.left, y: clientY - box.top }
    }

    function ensureOverlays() {
      if (!shieldEl) {
        shieldEl = document.createElement('div')
        shieldEl.className = 'workspace__drag-shield'
        document.body.appendChild(shieldEl)
      }
      if (!chipEl) {
        chipEl = document.createElement('div')
        chipEl.className = 'workspace__drag-chip'
        chipEl.hidden = true
        document.body.appendChild(chipEl)
      }
      if (!previewEl) {
        previewEl = document.createElement('div')
        previewEl.className = 'workspace__drop-preview'
        contentElRef.current?.appendChild(previewEl)
      }
    }
    function removeOverlays() {
      shieldEl?.remove(); shieldEl = null
      chipEl?.remove(); chipEl = null
      previewEl?.remove(); previewEl = null
    }

    function positionChip(clientX, clientY, isTouch, key) {
      if (!chipEl) return
      const { left, top } = chipOffset({ x: clientX, y: clientY }, isTouch)
      chipEl.style.left = `${left}px`
      chipEl.style.top = `${top}px`
      if (chipEl.hidden) {
        const tab = tabFromKey(key)
        const label = (labelForTabRef.current && tab) ? labelForTabRef.current(tab) : ''
        chipEl.textContent = label
        chipEl.hidden = false
      }
    }

    // Render (or clear) the drop preview for a zone. Geometry is written inline
    // (content-local px); appearance + morph transitions come from the CSS.
    function renderPreview(zone, prevZone) {
      if (!previewEl) return
      if (!zone) { previewEl.classList.remove('is-visible'); return }
      // Toggle the morph class only when the zone identity changes, so a
      // same-zone reposition doesn't re-trigger the first-appear fade.
      previewEl.classList.toggle('workspace__drop-preview--caret', zone.type === 'strip')
      const { rect } = zone
      previewEl.style.left = `${rect.x}px`
      previewEl.style.top = `${rect.y}px`
      previewEl.style.width = `${rect.w}px`
      previewEl.style.height = `${rect.h}px`
      previewEl.classList.add('is-visible')
    }

    // Pre-glow every eligible (visible) pane for 400ms on drag start (§3.3).
    function preGlow(scene) {
      const host = contentElRef.current
      if (!host) return
      for (const pane of scene.panes) {
        const g = document.createElement('div')
        g.className = 'workspace__drop-preglow'
        g.style.left = `${pane.rect.x}px`
        g.style.top = `${pane.rect.y}px`
        g.style.width = `${pane.rect.w}px`
        g.style.height = `${pane.rect.h}px`
        host.appendChild(g)
        requestAnimationFrame(() => g.classList.add('is-on'))
        setTimeout(() => { g.classList.remove('is-on'); setTimeout(() => g.remove(), 420) }, 420)
      }
    }

    // Measure a pane's strip tab rects (content-local) for the caret index.
    function measureTabs(paneId) {
      const host = contentElRef.current
      const strip = host?.querySelector(`[data-pane-strip="${cssEscape(paneId)}"]`)
      if (!strip) return []
      const box = contentBox()
      return [...strip.querySelectorAll('.shell__tab')].map((el) => {
        const r = el.getBoundingClientRect()
        return { left: r.left - box.left, right: r.right - box.left }
      })
    }

    function buildSceneNow(source, allowRootEdge) {
      const { projection, mode, contentRect } = sceneInputsRef.current
      const ws = workspaceStateRef.current.ws
      return buildScene(ws, projection, mode, contentRect, source, allowRootEdge, measureTabs)
    }

    function commitDrop(key, zone) {
      const target = zoneTarget(zone)
      if (!target) return
      const tab = tabFromKey(key)
      if (!tab) return
      const label = (labelForTabRef.current) ? labelForTabRef.current(tab) : 'tab'
      const before = workspaceStateRef.current.ws
      dispatchWorkspace({ type: 'OPEN_TAB_AT', tab, target, label: `Moved ${label}` })
      // Shell's dispatch advances workspaceStateRef synchronously, so a genuine
      // change is observable now — only toast (and only offer Undo) when the
      // workspace actually moved (a self-drop no-op stays silent).
      if (workspaceStateRef.current.ws !== before) showUndoToast(`Moved ${label}`)
    }

    // ── One drag session ──────────────────────────────────────────────────────
    function startSession(downEvent, srcEl, sourceKind, key, paneId) {
      const isTouch = downEvent.pointerType !== 'mouse'
      const start = { x: downEvent.clientX, y: downEvent.clientY }
      const pointerId = downEvent.pointerId
      let armed = false
      let cancelled = false
      let holdTimer = null
      let curZone = null
      let scene = null
      let drawerEdgeX = null
      let glided = false
      let prevBodySelect = ''
      let ctxListener = null

      const arm = () => {
        if (cancelled) return
        armed = true
        dragActiveRef.current = true // the Drawer's swipe-close handlers stand down
        onDragStart?.() // dismiss the coachmark
        try { srcEl.setPointerCapture?.(pointerId) } catch { /* capture optional */ }
        // Callout/selection suppression for the whole hold (§3.1).
        prevBodySelect = document.body.style.userSelect
        document.body.style.userSelect = 'none'
        document.body.style.webkitUserSelect = 'none'
        srcEl.style.webkitTouchCallout = 'none'
        const allowRootEdge = !isTouch && sceneInputsRef.current.mode !== 'phone'
        const source = {
          key,
          paneId,
          paneTabCount: paneId
            ? (workspaceStateRef.current.ws.panes[paneId]?.tabs.length || 0)
            : 0,
        }
        scene = buildSceneNow(source, allowRootEdge)
        ensureOverlays()
        positionChip(start.x, start.y, isTouch, key)
        preGlow(scene)
        if (isTouch && navigator.vibrate) { try { navigator.vibrate(10) } catch { /* unsupported */ } }
        if (sourceKind === 'drawer') {
          drawerEdgeX = document.getElementById('navigation-drawer')?.getBoundingClientRect().right ?? null
        }
      }

      // Touch lift is a long-press; a pre-hold move yields to native scroll.
      if (isTouch) holdTimer = setTimeout(arm, holdMsFor(sourceKind))

      const onMove = (ev) => {
        const dx = ev.clientX - start.x
        const dy = ev.clientY - start.y
        if (!armed) {
          if (isTouch) {
            if (preHoldMoveCancels(dx, dy)) { cancelled = true; clearTimeout(holdTimer); cleanup() }
            return
          }
          if (passedSlop(dx, dy)) arm()
          if (!armed) return
        }
        ev.preventDefault?.()
        positionChip(ev.clientX, ev.clientY, isTouch, key)

        // Drawer drag-out: while the chip is still over the drawer, nothing
        // arms; crossing 24px past the drawer's inner edge glides it closed and
        // reveals the panes' drop zones underneath (design §3.1).
        const overDrawer = sourceKind === 'drawer' && drawerEdgeX != null
          && (drawerOpenRef.current || ev.clientX <= drawerEdgeX)
        if (sourceKind === 'drawer' && !glided && drawerEdgeX != null
            && crossedDrawerExit(ev.clientX, drawerEdgeX)) {
          glided = true
          closeDrawer?.()
        }
        if (overDrawer && !glided) { curZone = null; renderPreview(null); return }

        const pt = toLocal(ev.clientX, ev.clientY)
        const next = hitTest(pt, scene, curZone)
        // The preview morphs (CSS transition on geometry) on a same-element move
        // and snaps class on a zone-kind change; renderPreview handles both.
        renderPreview(next, curZone)
        curZone = next
      }

      const onUp = (ev) => {
        clearTimeout(holdTimer)
        if (armed) {
          const dx = ev.clientX - start.x
          const dy = ev.clientY - start.y
          // Releasing back over the drawer cancels (design §3.1).
          const backOverDrawer = sourceKind === 'drawer' && drawerEdgeX != null
            && ev.clientX <= drawerEdgeX && drawerOpenRef.current
          if (isTouch && releasedInPlace(dx, dy)) {
            // Lift → release-in-place = context menu (strip tabs reuse the stage-A
            // menu; a drawer row keeps its own ⋮ menu, so it just cancels).
            if (sourceKind === 'tab' && openTabMenuAtRef.current) {
              const tab = tabFromKey(key)
              openTabMenuAtRef.current(ev.clientX, ev.clientY, tab, paneId)
            }
          } else if (!backOverDrawer && curZone) {
            commitDrop(key, curZone)
          }
        }
        cleanup()
      }

      const onCancel = () => { clearTimeout(holdTimer); cleanup() }
      const onKey = (ev) => { if (ev.key === 'Escape' && armed) { ev.preventDefault(); cleanup() } }

      function cleanup() {
        window.removeEventListener('pointermove', onMove, true)
        window.removeEventListener('pointerup', onUp, true)
        window.removeEventListener('pointercancel', onCancel, true)
        window.removeEventListener('keydown', onKey, true)
        if (ctxListener) { window.removeEventListener('contextmenu', ctxListener, true); ctxListener = null }
        if (armed) {
          try { srcEl.releasePointerCapture?.(pointerId) } catch { /* released */ }
          document.body.style.userSelect = prevBodySelect
          document.body.style.webkitUserSelect = prevBodySelect
          srcEl.style.webkitTouchCallout = ''
        }
        dragActiveRef.current = false
        removeOverlays()
      }

      // During a touch hold, the iOS callout/contextmenu must not win the gesture.
      if (isTouch) {
        ctxListener = (ev) => ev.preventDefault()
        window.addEventListener('contextmenu', ctxListener, true)
      }
      window.addEventListener('pointermove', onMove, { passive: false, capture: true })
      window.addEventListener('pointerup', onUp, true)
      window.addEventListener('pointercancel', onCancel, true)
      window.addEventListener('keydown', onKey, true)
    }

    // ── Source detection (capture-phase, never preventDefault here) ───────────
    function onPointerDown(e) {
      if (e.pointerType === 'mouse' && e.button !== 0) return
      if (!e.isPrimary) return
      const srcEl = e.target?.closest?.('[data-drag-key]')
      if (!srcEl) return
      const key = srcEl.dataset.dragKey
      if (!key) return
      const inDrawer = srcEl.closest('#navigation-drawer')
      const strip = srcEl.closest('[data-pane-strip]')
      const sourceKind = inDrawer ? 'drawer' : (strip ? 'tab' : null)
      if (!sourceKind) return
      const paneId = strip ? strip.dataset.paneStrip : null
      startSession(e, srcEl, sourceKind, key, paneId)
    }

    document.addEventListener('pointerdown', onPointerDown, true)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true)
      removeOverlays()
    }
    // enabled is a module-load constant and every volatile input arrives through
    // a ref, so the listener installs exactly once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled])
}

function cssEscape(v) {
  return (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape(String(v)) : String(v)
}
