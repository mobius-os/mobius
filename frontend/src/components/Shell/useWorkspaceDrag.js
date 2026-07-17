import { useEffect } from 'react'
import * as tabModel from './tabModel.js'
import { STRIP_H } from './paneModel.js'
import {
  buildScene, hitTest, zoneTarget, releaseZone, chipOffset, STRIP_CARET_PAD,
  passedSlop, preHoldMoveCancels, releasedInPlace, holdMsFor, crossedDrawerExit,
  rootEdgeAllowed,
} from './dragController.js'

// The thin React binding for the workspace drag controller (design §3). It owns
// only the side-effects the pure dragController.js cannot: pointer capture, the
// hold/slop timers, the chip/preview/shield/pre-glow DOM, the drawer stand-down,
// strip auto-scroll, and the ONE reducer dispatch on drop. Every geometric
// decision is delegated to the pure module, so this file has no thresholds or
// zone math of its own.
//
// Architecture (design §3.1): a single capture-phase `pointerdown` on the
// document identifies a drag source by its `data-drag-key` (strip tabs in the
// chrome AND the single-pane top strip, rows in the drawer) and geometrically
// hit-tests against projectLayout rects — never DOM-event bubbling, because
// iframes swallow events and the drawer is inert. Everything is gated behind
// `enabled` (WORKSPACE_SPLITS_ENABLED); with the flag off the listener never
// installs and the shell is unchanged.

const STRIP_ZONE_H = STRIP_H + STRIP_CARET_PAD
const AUTO_SCROLL_EDGE = 32 // px from a strip's scroll edge that arms auto-scroll
const AUTO_SCROLL_STEP = 6 // px/frame

// Split a tab key ("chat:5" / "app:42") back into a tabModel tab.
function tabFromKey(key) {
  const i = key.indexOf(':')
  if (i < 0) return null
  return tabModel.makeTab(key.slice(0, i), key.slice(i + 1))
}

function cssEscape(v) {
  return (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape(String(v)) : String(v)
}

// A one-shot capture-phase click swallow — the pointer-capture compat click
// lands on the original source AFTER the shield is gone, so shield timing can't
// stop it. Scope the guard to that source: a real drag often produces no compat
// click at all, and a blanket "next click" guard would eat a quick Undo or other
// unrelated action during this short window.
function suppressNextSourceClick(sourceEl) {
  let cleared = false
  const clear = () => {
    if (cleared) return
    cleared = true
    window.removeEventListener('click', onClick, true)
    clearTimeout(timer)
  }
  const onClick = (ev) => {
    const path = typeof ev.composedPath === 'function' ? ev.composedPath() : []
    const belongsToSource = path.includes(sourceEl)
      || ev.target === sourceEl
      || sourceEl?.contains?.(ev.target)
    if (!belongsToSource) return
    ev.stopPropagation()
    ev.preventDefault()
    clear()
  }
  window.addEventListener('click', onClick, true)
  const timer = setTimeout(clear, 400)
}

export default function useWorkspaceDrag({
  enabled,
  contentElRef,
  sceneInputsRef, // ref → { projection, mode, contentRect }
  workspaceStateRef, // ref → { ws, undo } (advanced synchronously by Shell's dispatch)
  dispatchWorkspace,
  labelForTabRef, // ref → (tab) => string
  dragActiveRef, // shared flag the Drawer's swipe-close handlers stand down on
  drawerOpenRef,
  closeDrawer,
  openDrawer,
  openTabMenuAtRef, // ref → (clientX, clientY, tab, paneId) => void
  onDragStart, // dismiss the coachmark on the first real drag
}) {
  useEffect(() => {
    if (!enabled) return undefined

    // ── Reusable overlay DOM (created lazily on the first arm) ────────────────
    let shieldEl = null
    let chipEl = null
    let previewEl = null
    // The one in-flight session's teardown, so an unmount / disable can tear a
    // live drag down cleanly — no orphaned shield.
    let activeCleanup = null

    function contentBox() {
      return contentElRef.current?.getBoundingClientRect() || { left: 0, top: 0 }
    }
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
    function renderPreview(zone) {
      if (!previewEl) return
      if (!zone) { previewEl.classList.remove('is-visible'); return }
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

    // ── One drag session ──────────────────────────────────────────────────────
    function startSession(downEvent, srcEl, sourceKind, key, paneId) {
      const isTouch = downEvent.pointerType !== 'mouse'
      const start = { x: downEvent.clientX, y: downEvent.clientY }
      const pointerId = downEvent.pointerId
      let armed = false
      let cancelled = false
      let cleaned = false
      let holdTimer = null
      let curZone = null
      let scene = null
      let drawerEdgeX = null
      let glided = false
      let prevBodySelect = ''
      let lastPoint = { x: start.x, y: start.y }
      // Auto-scroll (§3.2) state.
      let autoRAF = null
      let autoStripEl = null
      let autoPaneId = null
      let autoDir = 0

      const buildSource = () => ({
        key,
        paneId,
        paneTabCount: paneId
          ? (workspaceStateRef.current.ws.panes[paneId]?.tabs.length || 0)
          : 0,
      })

      // iOS callout/selection suppression begins NOW (pointerdown), scoped to the
      // source, for the WHOLE hold window — not at arm, when the magnifier has
      // already won. `contextmenu` is prevented for a touch source too.
      let ctxListener = null
      let touchMovePreventer = null
      if (isTouch) {
        prevBodySelect = document.body.style.userSelect
        document.body.style.userSelect = 'none'
        document.body.style.webkitUserSelect = 'none'
        srcEl.style.webkitTouchCallout = 'none'
        srcEl.style.userSelect = 'none'
        ctxListener = (ev) => ev.preventDefault()
        window.addEventListener('contextmenu', ctxListener, true)
        // Dynamic touch-action mid-gesture is ignored by the browser; a
        // non-passive touchmove that preventDefaults ONLY while armed is what
        // actually blocks Android `pan-y` native scrolling after lift, while
        // pre-hold movement still scrolls.
        touchMovePreventer = (ev) => { if (armed) ev.preventDefault() }
        document.addEventListener('touchmove', touchMovePreventer, { passive: false })
      }

      const arm = () => {
        if (cancelled || cleaned) return
        armed = true
        dragActiveRef.current = true // the Drawer's swipe-close handlers stand down
        onDragStart?.() // dismiss the coachmark
        try { srcEl.setPointerCapture?.(pointerId) } catch { /* capture optional */ }
        if (!isTouch) {
          prevBodySelect = document.body.style.userSelect
          document.body.style.userSelect = 'none'
          document.body.style.webkitUserSelect = 'none'
        }
        const allowRootEdge = rootEdgeAllowed(isTouch, sceneInputsRef.current.mode)
        scene = buildSceneNow(buildSource(), allowRootEdge)
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

      function stopAutoScroll() {
        if (autoRAF) { cancelAnimationFrame(autoRAF); autoRAF = null }
        autoDir = 0
        autoStripEl = null
        autoPaneId = null
      }
      // Strip auto-scroll (§3.2): near an overflowing strip's edge, scroll it
      // 6px/frame and re-measure so the caret keeps tracking under the pointer.
      function updateAutoScroll(clientX, clientY) {
        const box = contentBox()
        const xL = clientX - box.left
        const yL = clientY - box.top
        let stripEl = null
        let dir = 0
        let pid = null
        for (const pane of scene.panes) {
          const r = pane.rect
          if (xL < r.x || xL > r.x + r.w || yL < r.y || yL > r.y + STRIP_ZONE_H) continue
          const el = contentElRef.current?.querySelector(`[data-pane-strip="${cssEscape(pane.paneId)}"]`)
          pid = pane.paneId
          if (el && el.scrollWidth > el.clientWidth + 1) {
            const sb = el.getBoundingClientRect()
            if (clientX < sb.left + AUTO_SCROLL_EDGE && el.scrollLeft > 0) { stripEl = el; dir = -1 }
            else if (clientX > sb.right - AUTO_SCROLL_EDGE
              && el.scrollLeft < el.scrollWidth - el.clientWidth) { stripEl = el; dir = 1 }
          }
          break
        }
        autoStripEl = stripEl
        autoPaneId = pid
        autoDir = dir
        if (dir !== 0 && !autoRAF) autoRAF = requestAnimationFrame(autoStep)
        else if (dir === 0) stopAutoScroll()
      }
      function autoStep() {
        if (!armed || autoDir === 0 || !autoStripEl) { autoRAF = null; return }
        autoStripEl.scrollLeft += autoDir * AUTO_SCROLL_STEP
        const p = scene?.panes.find(pp => pp.paneId === autoPaneId)
        if (p) p.tabs = measureTabs(autoPaneId) // re-measure the scrolled strip
        const next = hitTest(toLocal(lastPoint.x, lastPoint.y), scene, curZone)
        renderPreview(next)
        curZone = next
        autoRAF = requestAnimationFrame(autoStep)
      }

      // The per-frame drag work (chip follow, hit-test, preview, auto-scroll) is
      // rAF-coalesced: a pointermove fires several times per frame, and each pass
      // writes the fixed chip, forces a layout read (contentBox), allocates a
      // fresh hit-test result, and writes preview styles. Batching to one pass per
      // frame drops the forced-reflow + GC churn to at most one per frame.
      let moveRAF = 0
      const doMoveWork = () => {
        moveRAF = 0
        if (!armed || cleaned) return
        const { x: cx, y: cy } = lastPoint
        positionChip(cx, cy, isTouch, key)
        // While the drawer still covers the panes, show no preview (the glide-close
        // crossing is handled synchronously in onMove).
        if (sourceKind === 'drawer' && drawerOpenRef.current) {
          curZone = null; renderPreview(null); stopAutoScroll(); return
        }
        updateAutoScroll(cx, cy)
        const next = hitTest(toLocal(cx, cy), scene, curZone)
        renderPreview(next)
        curZone = next
      }
      const onMove = (ev) => {
        if (ev.pointerId !== pointerId) return // ignore a second finger
        lastPoint = { x: ev.clientX, y: ev.clientY }
        const dx = ev.clientX - start.x
        const dy = ev.clientY - start.y
        if (!armed) {
          if (isTouch) {
            if (preHoldMoveCancels(dx, dy)) { cancelled = true; cleanup() }
            return
          }
          if (passedSlop(dx, dy)) arm()
          if (!armed) return
        }
        ev.preventDefault?.()
        // Drawer drag-out glide-close must fire SYNCHRONOUSLY (it dispatches
        // closeDrawer and stands the OS gesture down); the heavy hit-test/preview
        // work is deferred to the coalesced rAF pass above (design §3.1).
        if (sourceKind === 'drawer' && drawerEdgeX != null && !glided
            && crossedDrawerExit(ev.clientX, drawerEdgeX)) {
          glided = true
          closeDrawer?.()
        }
        if (!moveRAF) moveRAF = requestAnimationFrame(doMoveWork)
      }

      // Re-derive the drop against a FRESH scene at the release point: geometry
      // or caps may have shifted since arm (resize, a concurrent placement), and
      // a stale-lit zone must not commit an infeasible move (TOCTOU).
      // A zone that flipped infeasible resolves to null and the drop cancels
      // visibly (the chip/preview animate away), never a silent reducer no-op.
      function commitDrop() {
        const allowRootEdge = rootEdgeAllowed(isTouch, sceneInputsRef.current.mode)
        const fresh = buildSceneNow(buildSource(), allowRootEdge)
        // Commit ONLY the operation the preview promised: releaseZone returns the
        // fresh zone iff it is structurally identical to the previewed one, else
        // null so a zone that flipped infeasible between the last move and the
        // release cancels rather than silently committing a different mutation.
        const zone = releaseZone(hitTest(toLocal(lastPoint.x, lastPoint.y), fresh, curZone), curZone)
        const target = zoneTarget(zone)
        if (!target) return
        const tab = tabFromKey(key)
        if (!tab) return
        const label = labelForTabRef.current ? labelForTabRef.current(tab) : 'tab'
        // The undo toast is driven by the reducer's undo slot (Shell), not raised
        // here: OPEN_TAB_AT stamps a `toast` on the slot only when the drop
        // actually mutates, so the toast can never outlive or mis-name its
        // snapshot (design §3.5).
        dispatchWorkspace({ type: 'OPEN_TAB_AT', tab, target, label: `Moved ${label}` })
      }

      const onUp = (ev) => {
        if (ev.pointerId !== pointerId) return // ignore a second finger
        if (!armed) { cleanup(); return }
        const dx = ev.clientX - start.x
        const dy = ev.clientY - start.y
        // Releasing over the drawer's original region cancels — and, if the drag
        // had already glided it closed, reopens it (design §3.1/§3.4).
        // Geometric, so it no longer depends on the drawer still reporting open
        // (which glide-close had already flipped false).
        const backOverDrawer = sourceKind === 'drawer' && drawerEdgeX != null
          && ev.clientX <= drawerEdgeX
        if (isTouch && releasedInPlace(dx, dy)) {
          // Lift → release-in-place = context menu. A strip tab reuses the
          // stage-A pane menu; a drawer row opens its own ⋮ menu (adjudicated).
          if (sourceKind === 'tab' && openTabMenuAtRef.current) {
            openTabMenuAtRef.current(ev.clientX, ev.clientY, tabFromKey(key), paneId)
          } else if (sourceKind === 'drawer') {
            srcEl.closest('.drawer__row')?.querySelector('.drawer__more')?.click()
          }
          cleanup()
        } else if (backOverDrawer) {
          // Released back over the drawer = cancel; cleanup reopens it if glided.
          cleanup({ suppressClick: true })
        } else {
          if (curZone) commitDrop()
          // A release over a live zone is a commit (drawer stays closed); a
          // release over NO zone is a cancel (cleanup reopens a glided drawer).
          cleanup({ suppressClick: true, committed: !!curZone })
        }
      }

      const onCancel = (ev) => { if (ev.pointerId === pointerId) cleanup() }
      const onKey = (ev) => { if (ev.key === 'Escape' && armed) { ev.preventDefault(); cleanup() } }
      const onLostCapture = (ev) => { if (ev.pointerId === pointerId) cleanup() }
      const onWinBlur = () => cleanup()
      const onVisibility = () => { if (document.visibilityState === 'hidden') cleanup() }

      function cleanup({ suppressClick = false, committed = false } = {}) {
        if (cleaned) return
        cleaned = true
        clearTimeout(holdTimer)
        if (moveRAF) { cancelAnimationFrame(moveRAF); moveRAF = 0 }
        stopAutoScroll()
        window.removeEventListener('pointermove', onMove, true)
        window.removeEventListener('pointerup', onUp, true)
        window.removeEventListener('pointercancel', onCancel, true)
        window.removeEventListener('keydown', onKey, true)
        window.removeEventListener('lostpointercapture', onLostCapture, true)
        window.removeEventListener('blur', onWinBlur)
        document.removeEventListener('visibilitychange', onVisibility)
        if (ctxListener) window.removeEventListener('contextmenu', ctxListener, true)
        if (touchMovePreventer) document.removeEventListener('touchmove', touchMovePreventer)
        // Restore selection/callout (set at pointerdown for touch, at arm for mouse).
        if (isTouch || armed) {
          document.body.style.userSelect = prevBodySelect
          document.body.style.webkitUserSelect = prevBodySelect
        }
        srcEl.style.webkitTouchCallout = ''
        srcEl.style.userSelect = ''
        try { srcEl.releasePointerCapture?.(pointerId) } catch { /* released */ }
        dragActiveRef.current = false
        // Glide-close is provisional (design §3.1 — nothing mutates until drop).
        // A session that ends WITHOUT a committed drop — Escape, pointercancel,
        // window blur, visibility loss, lost capture, a release over no zone, or a
        // release back over the drawer — must restore the drawer it glided shut.
        if (glided && !committed && sourceKind === 'drawer' && !drawerOpenRef.current) {
          openDrawer?.()
        }
        removeOverlays()
        // The compat click fires after the shield is already gone; swallow it so
        // a committed drop is exactly one action, not a drop + a tab/row click.
        if (suppressClick) suppressNextSourceClick(srcEl)
        if (activeCleanup === cleanup) activeCleanup = null
      }
      activeCleanup = cleanup

      window.addEventListener('pointermove', onMove, { passive: false, capture: true })
      window.addEventListener('pointerup', onUp, true)
      window.addEventListener('pointercancel', onCancel, true)
      window.addEventListener('keydown', onKey, true)
      window.addEventListener('lostpointercapture', onLostCapture, true)
      window.addEventListener('blur', onWinBlur)
      document.addEventListener('visibilitychange', onVisibility)
    }

    // ── Source detection (capture-phase, never preventDefault here) ───────────
    function onPointerDown(e) {
      if (activeCleanup) return // one session at a time
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
      activeCleanup?.() // tear down an in-flight drag
      removeOverlays()
    }
    // enabled is a module-load constant and every volatile input arrives through
    // a ref, so the listener installs exactly once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled])
}
