import { useEffect } from 'react'
import * as tabModel from './tabModel.js'
import { STRIP_H } from './paneModel.js'
import {
  buildScene, hitTest, zoneTarget, releaseZone, chipOffset, STRIP_CARET_PAD,
  passedSlop, touchMoveIntent, releasedInPlace, holdMsFor, crossedDrawerExit,
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
  return clear
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
  onPreviewBuilder, // (active, { committed }) => void — enter/leave the LIVE
  // builder preview a single-mode drag unfolds (point 15: dragging IS
  // building). Render-only: the reducer viewMode stays 'single' until the drop
  // commits 'panes', so ONE undo reverts both the tree AND the mode. The leave
  // call carries the outcome — committed:true means the drop's OPEN_TAB_AT
  // flipped the tree, so the descriptor must drag-commit (not cancel) in the
  // same batch. (Settings needs no conversion across the flip — its tab survives;
  // single mode paints its own slot, never a forced takeover.)
}) {
  useEffect(() => {
    if (!enabled) return undefined

    // ── Reusable overlay DOM (created lazily on the first arm) ────────────────
    let shieldEl = null
    let chipEl = null
    let previewEl = null
    // The one in-flight session's teardown, so an unmount / disable can tear a
    // live drag down cleanly — no orphaned shield. activePointerId / activeSrcEl
    // travel with it so the next-interaction reconcile (below) can tell whether the
    // standing session's pointer is still LIVE (holds capture) or dead.
    let activeCleanup = null
    let activePointerId = null
    let activeSrcEl = null
    // A drag-owned compatibility-click guard belongs to exactly one completed
    // pointer gesture. A fresh pointerdown is proof that a later owner gesture
    // has begun, so it must retire any old guard before that gesture's click.
    // Without this boundary, an interrupted drag followed quickly by a tap on
    // the same drawer row made the row look dead for up to 400ms.
    let clearPendingSourceClick = null

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
    // Pre-glow nodes are appended separately from the shield/chip/preview and
    // self-remove ~840ms later; a cancelled drag must take them down NOW too, else
    // they linger (review §12). Their scheduled timers are cleared so the delayed
    // remove() can't fire on an already-detached node.
    const preGlowNodes = []
    function clearPreGlow() {
      for (const { node, timers } of preGlowNodes) {
        for (const id of timers) clearTimeout(id)
        node.remove()
      }
      preGlowNodes.length = 0
    }

    function removeOverlays() {
      shieldEl?.remove(); shieldEl = null
      chipEl?.remove(); chipEl = null
      previewEl?.remove(); previewEl = null
      clearPreGlow()
    }

    function positionChip(clientX, clientY, isTouch, key) {
      if (!chipEl) return
      // Set the label + reveal FIRST so offsetWidth is accurate before we clamp.
      if (chipEl.hidden) {
        const tab = tabFromKey(key)
        const label = (labelForTabRef.current && tab) ? labelForTabRef.current(tab) : ''
        chipEl.textContent = label
        chipEl.hidden = false
      }
      const { left, top } = chipOffset({ x: clientX, y: clientY }, isTouch)
      // V5 (vizreview): clamp the chip within the viewport so its label never clips
      // at the right edge (the +12 offset pushed a right-edge drag off-screen).
      const margin = 8
      const w = chipEl.offsetWidth || 0
      const maxLeft = Math.max(margin, window.innerWidth - w - margin)
      chipEl.style.left = `${Math.max(margin, Math.min(left, maxLeft))}px`
      chipEl.style.top = `${top}px`
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
        const t1 = setTimeout(() => {
          g.classList.remove('is-on')
          const t2 = setTimeout(() => g.remove(), 420)
          entry.timers.push(t2)
        }, 420)
        const entry = { node: g, timers: [t1] }
        preGlowNodes.push(entry)
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
      const touchIntentKind = sourceKind === 'tab'
        && downEvent.target?.closest?.('[data-touch-drag-handle]')
        ? 'tab-handle'
        : sourceKind
      const start = { x: downEvent.clientX, y: downEvent.clientY }
      const pointerId = downEvent.pointerId
      let armed = false
      let cancelled = false
      let cleaned = false
      let holdTimer = null
      let held = false
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
      if (isTouch) {
        prevBodySelect = document.body.style.userSelect
        document.body.style.userSelect = 'none'
        document.body.style.webkitUserSelect = 'none'
        srcEl.style.webkitTouchCallout = 'none'
        srcEl.style.userSelect = 'none'
        ctxListener = (ev) => ev.preventDefault()
        window.addEventListener('contextmenu', ctxListener, true)
      }

      const arm = () => {
        if (armed || cancelled || cleaned) return
        armed = true
        dragActiveRef.current = true // the Drawer's swipe-close handlers stand down
        // DRAG IS BUILDING (point 15): arming a drag in single-screen mode unfolds
        // the builder world LIVE — the parked multi-pane layout (or the lone leaf as
        // one pane) tiles in, and the normal drop zones apply. This is a RENDER-only
        // preview; the reducer viewMode stays 'single', so a cancel reverts with no
        // mutation, and a committed drop flips 'panes' via OPEN_TAB_AT (one undo
        // reverts both). There is no drag-deny anymore — dragging is always allowed.
        if (workspaceStateRef.current.ws.viewMode === 'single') onPreviewBuilder?.(true)
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
        if (isTouch && !held && navigator.vibrate) { try { navigator.vibrate(10) } catch { /* unsupported */ } }
        if (sourceKind === 'drawer') {
          drawerEdgeX = document.getElementById('navigation-drawer')?.getBoundingClientRect().right ?? null
        }
      }

      // A vertical tab-body move, either-axis kind-icon move, or cross-axis drawer-row
      // move arms immediately. A stationary hold is the alternate path to the
      // tab/row menu; it deliberately does not unfold the workspace just because
      // time passed.
      if (isTouch) {
        holdTimer = setTimeout(() => {
          if (cancelled || cleaned) return
          held = true
          if (navigator.vibrate) { try { navigator.vibrate(8) } catch { /* unsupported */ } }
        }, holdMsFor(sourceKind))
      }

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
        if (sourceKind === 'drawer' && drawerOpenRef.current && !glided) {
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
            const intent = touchMoveIntent(dx, dy, touchIntentKind)
            if (intent === 'scroll') { cancelled = true; cleanup(); return }
            if (intent === 'drag') {
              clearTimeout(holdTimer)
              arm()
            }
            if (!armed) return
          } else {
            if (passedSlop(dx, dy)) arm()
            if (!armed) return
          }
        }
        ev.preventDefault?.()
        // Drawer drag-out glide-close must fire SYNCHRONOUSLY (it dispatches
        // closeDrawer and stands the OS gesture down); the heavy hit-test/preview
        // work is deferred to the coalesced rAF pass above (design §3.1).
        if (sourceKind === 'drawer' && drawerOpenRef.current
            && drawerEdgeX != null && !glided
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
        if (!target) return false
        const tab = tabFromKey(key)
        if (!tab) return false
        const label = labelForTabRef.current ? labelForTabRef.current(tab) : 'tab'
        // The undo toast is driven by the reducer's undo slot (Shell), not raised
        // here: OPEN_TAB_AT stamps a `toast` on the slot only when the drop
        // actually mutates, so the toast can never outlive or mis-name its
        // snapshot (design §3.5).
        // DRAG IS BUILDING (point 15): ANY drop made from single-screen mode commits
        // builder mode — you built something, you stay in the build world. Fold the
        // 'panes' flip INTO the OPEN_TAB_AT payload so the drop and the flip are ONE
        // undoable gesture (restoreViewMode reverts BOTH the tree and viewMode to
        // 'single'; a following SET_VIEW_MODE would leave a half-undone gesture). The
        // reducer viewMode is still 'single' here — the builder unfold was a
        // render-only preview — so undo.ws captures 'single' correctly. This folds in
        // the former single-leaf split-drop flip as the no-parked-layout case.
        const before = workspaceStateRef.current.ws
        const flipToPanes = before.viewMode === 'single'
        // Settings needs no conversion across the flip: a builder Settings tab
        // survives, and single mode paints its own slot rather than a takeover, so a
        // drop-into-builder no longer routes any overlay<->tab conversion.
        dispatchWorkspace({
          type: 'OPEN_TAB_AT', tab, target, label: `Moved ${label}`,
          flipViewMode: flipToPanes ? 'panes' : null,
        })
        // §8: "committed" is whether the workspace ACTUALLY changed (a same-slot
        // no-op leaves it untouched), not merely that a zone was lit — the caller
        // uses this to decide drawer restoration.
        return workspaceStateRef.current.ws !== before
      }

      const onUp = (ev) => {
        if (ev.pointerId !== pointerId) return // ignore a second finger
        const openTouchMenu = () => {
          if (sourceKind === 'tab' && openTabMenuAtRef.current) {
            openTabMenuAtRef.current(ev.clientX, ev.clientY, tabFromKey(key), paneId)
          } else if (sourceKind === 'drawer') {
            srcEl.closest('.drawer__row')?.querySelector('.drawer__more')?.click()
          }
        }
        if (!armed) {
          if (isTouch && held) {
            openTouchMenu()
            cleanup({ suppressClick: true })
          } else cleanup()
          return
        }
        if (moveRAF) {
          cancelAnimationFrame(moveRAF)
          doMoveWork()
        }
        const dx = ev.clientX - start.x
        const dy = ev.clientY - start.y
        // Releasing over the drawer's original region cancels — and, if the drag
        // had already glided it closed, reopens it (design §3.1/§3.4).
        // Geometric, so it no longer depends on the drawer still reporting open
        // (which glide-close had already flipped false).
        const backOverDrawer = sourceKind === 'drawer' && drawerEdgeX != null
          && ev.clientX <= drawerEdgeX && !(isTouch && glided)
        if (isTouch && releasedInPlace(dx, dy)) {
          // Lift → release-in-place = context menu. A strip tab reuses the
          // stage-A pane menu; a drawer row opens its own ⋮ menu (adjudicated).
          openTouchMenu()
          cleanup()
        } else if (backOverDrawer) {
          // Released back over the drawer = cancel; cleanup reopens it if glided.
          cleanup({ suppressClick: true })
        } else {
          // "committed" is the ACTUAL dispatch outcome (§8) — a fresh-validation
          // cancel or a same-slot no-op leaves the workspace untouched and is treated
          // as a cancel (glided drawer restored). A live zone that really mutates
          // keeps the drawer closed.
          const didCommit = curZone ? commitDrop() : false
          cleanup({ suppressClick: true, committed: didCommit })
        }
      }

      // Every cancel path suppresses the trailing source click when a drag had
      // ARMED (§9): otherwise the compat click after an Escape / lost-capture /
      // blur / visibility cancel can still navigate to the source row.
      const onCancel = (ev) => { if (ev.pointerId === pointerId) cleanup({ suppressClick: armed }) }
      const onKey = (ev) => { if (ev.key === 'Escape' && armed) { ev.preventDefault(); cleanup({ suppressClick: true }) } }
      // Touch pointers already have implicit capture, and Chromium may release and
      // reacquire it while the strip updates without ending the contact. The window
      // listeners still receive that stream, so only pointerup/pointercancel is a
      // terminal touch signal. A mouse capture loss remains a real cancellation.
      const onLostCapture = (ev) => {
        if (ev.pointerId === pointerId && !isTouch) cleanup({ suppressClick: armed })
      }
      const onWinBlur = () => cleanup({ suppressClick: armed })
      const onVisibility = () => { if (document.visibilityState === 'hidden') cleanup({ suppressClick: armed }) }
      // BFCache freeze / bfcache navigation can be the ONLY interruption event some
      // browsers fire — no pointercancel, no blur, and (on older Safari) no
      // visibilitychange-hidden first. Without this, a drag frozen mid-flight and
      // then restored would keep its render-only builder preview, wedging the
      // workspace tiled. pagehide cancels the drag as the page is frozen/unloaded.
      const onPageHide = () => cleanup({ suppressClick: armed })

      function cleanup({ suppressClick = false, committed = false } = {}) {
        if (cleaned) return
        cleaned = true
        // Leave the live builder preview, telling the mode machine WHICH way it
        // ended. On a COMMITTED drop the reducer is now in 'panes' (OPEN_TAB_AT
        // flipped it) and the descriptor must commit in the SAME pointerup batch
        // (drag-commit → committedMode 'panes'; INV 7 one-transaction) — routing
        // a successful drop through drag-cancel left one committed render where
        // the tree said 'panes' but the descriptor painted single (preview
        // collapse + logo untwist) until the passive sync-committed net caught
        // up. On CANCEL the reducer never left 'single', so the cancel reverts
        // the render with no mutation.
        onPreviewBuilder?.(false, { committed })
        clearTimeout(holdTimer)
        if (moveRAF) { cancelAnimationFrame(moveRAF); moveRAF = 0 }
        stopAutoScroll()
        window.removeEventListener('pointermove', onMove, true)
        window.removeEventListener('pointerup', onUp, true)
        window.removeEventListener('pointercancel', onCancel, true)
        window.removeEventListener('keydown', onKey, true)
        window.removeEventListener('lostpointercapture', onLostCapture, true)
        window.removeEventListener('blur', onWinBlur)
        window.removeEventListener('pagehide', onPageHide)
        document.removeEventListener('visibilitychange', onVisibility)
        if (ctxListener) window.removeEventListener('contextmenu', ctxListener, true)
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
        // §7: glide-close used an async history.back(); its handleBack flips
        // drawerOpenRef false only when the traversal SETTLES, which can land AFTER
        // this cleanup. Reopening against a stale "still open" snapshot would be
        // clobbered by that pending close. So RECONCILE: wait (bounded) for the
        // pending close to settle, then reopen — never gate on the stale snapshot.
        if (glided && !committed && sourceKind === 'drawer') {
          const reopen = (attempts) => {
            if (drawerOpenRef.current) {
              if (attempts < 20) requestAnimationFrame(() => reopen(attempts + 1))
              return // the pending glide-close hasn't landed yet — wait a frame
            }
            openDrawer?.()
          }
          reopen(0)
        }
        removeOverlays()
        // The compat click fires after the shield is already gone; swallow it so
        // a committed drop is exactly one action, not a drop + a tab/row click.
        if (suppressClick) {
          clearPendingSourceClick?.()
          clearPendingSourceClick = suppressNextSourceClick(srcEl)
        }
        // V6 (vizreview): a CANCELLED drag (Escape / blur / lost-capture) must not
        // leave the drag-origin row wearing its focus ring — blur it so the ring
        // clears with the drag. A committed drop keeps focus (the tab moved).
        if (suppressClick && !committed) srcEl.blur?.()
        if (activeCleanup === cleanup) {
          activeCleanup = null
          activePointerId = null
          activeSrcEl = null
        }
      }
      activeCleanup = cleanup
      activePointerId = pointerId
      activeSrcEl = srcEl

      window.addEventListener('pointermove', onMove, { passive: false, capture: true })
      window.addEventListener('pointerup', onUp, true)
      window.addEventListener('pointercancel', onCancel, true)
      window.addEventListener('keydown', onKey, true)
      window.addEventListener('lostpointercapture', onLostCapture, true)
      window.addEventListener('blur', onWinBlur)
      window.addEventListener('pagehide', onPageHide)
      document.addEventListener('visibilitychange', onVisibility)
    }

    // Whether the standing session's pointer is still LIVE — it holds capture for
    // its own pointerId. A visible->visible interruption (partial notification-shade
    // occlusion, split-screen) can steal the pointer WITHOUT firing pointercancel /
    // blur / visibilitychange / pageshow, so neither the per-session teardown nor
    // the foreground reconcile fires and the session (with its dragPreviewBuilder
    // override) strands with no boundary to catch it. The one edge that always
    // follows is the user's NEXT interaction — a fresh pointerdown. If a session
    // stands but its pointer is dead, reconcile it before the new interaction
    // proceeds (still edge-triggered — no polling, no timers).
    function standingSessionPointerIsLive() {
      try { return !!(activeSrcEl && activeSrcEl.hasPointerCapture?.(activePointerId)) }
      catch { return false }
    }

    // ── Source detection (capture-phase, never preventDefault here) ───────────
    function onPointerDown(e) {
      // A compatibility click from the previous gesture cannot legitimately
      // begin with a new pointerdown. Clear its one-shot guard before doing any
      // stale-session reconciliation so this fresh interaction stays live.
      clearPendingSourceClick?.()
      clearPendingSourceClick = null
      if (activeCleanup) {
        // Pointer ids are routinely REUSED across sequential touch gestures
        // (notably id=1 on mobile). Liveness comes from capture, never identity:
        // if the standing source no longer owns capture, force-clean it and let
        // this SAME pointerdown continue into the row it actually targeted.
        // This boundary is already newer than the abandoned gesture, so arming
        // a click suppressor here would eat this interaction's own click.
        if (!standingSessionPointerIsLive()) {
          activeCleanup()
        } else {
          return // one session at a time
        }
      }
      // Primary-button-only: a non-primary mouse button never arms a drag. This
      // is also what lets middle-click-to-close a tab (PaneStrip's auxclick) be
      // safe — a middle press (button 1) returns here, so it can never start a
      // tab drag before the close fires.
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

    // Foreground reconcile (defensive — same class as the sleep/wake stream
    // reconcile, not a band-aid). A drag session cannot legitimately span a
    // visibility/foreground boundary: the per-session teardown above already
    // cancels a live drag as the tab LEAVES (visibilitychange->hidden, blur,
    // pagehide). So any session still standing at a visible/pageshow edge had its
    // going-out teardown SKIPPED (an exotic pointer-steal that fired none of those),
    // and its render-only builder PREVIEW (dragPreviewBuilder) would otherwise stay
    // true forever — the workspace stuck tiled after every later exit, matching the
    // "permanent stuck-tiled after an interrupted touch drag" report. Force it down,
    // then assert the override is off. A genuinely in-progress drag never receives
    // these edges (reaching `visible` requires a prior `hidden`, which already
    // cancelled it), so this never cancels a live drag — it only reconciles a stale
    // one, on the opposite edge from the teardown, so the two never double-handle.
    // A visible->visible steal that fires NEITHER edge is caught by the
    // next-interaction reconcile in onPointerDown. INVARIANT: the dragPreviewBuilder
    // override may outlive its session by at most ONE visibility/foreground boundary,
    // or at most one subsequent user interaction.
    function reconcileStaleSession() {
      // suppressClick so a late pointer-up / high-level click after the force-clean
      // cannot activate the original tab or drawer row (finding 4).
      activeCleanup?.({ suppressClick: true }) // full teardown (also clears the preview)
      onPreviewBuilder?.(false) // and assert the override is off whenever no session is live
    }
    const onForegroundVisible = () => {
      if (document.visibilityState === 'visible') reconcileStaleSession()
    }
    window.addEventListener('pageshow', reconcileStaleSession)
    document.addEventListener('visibilitychange', onForegroundVisible)

    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true)
      window.removeEventListener('pageshow', reconcileStaleSession)
      document.removeEventListener('visibilitychange', onForegroundVisible)
      activeCleanup?.() // tear down an in-flight drag
      clearPendingSourceClick?.()
      removeOverlays()
    }
    // enabled is a module-load constant and every volatile input arrives through
    // a ref, so the listener installs exactly once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled])
}
