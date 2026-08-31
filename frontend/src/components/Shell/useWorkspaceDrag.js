import { useEffect } from 'react'
import * as tabModel from './tabModel.js'
import {
  buildScene, hitTest, zoneTarget, releaseZone, chipOffset, STRIP_CARET_PAD,
  passedSlop, touchTabMoveIntent, drawerRowMoveIntent, releasedInPlace,
  flingReleaseVelocity,
  PRESS_DRAG_HOLD_MS, PRESS_MENU_HOLD_MS,
  crossedDrawerExit,
  rootEdgeAllowed,
} from './dragController.js'
import {
  captureLayoutSpace,
  clientLengthToLayout,
  clientPointToLayout,
} from '../../lib/layoutSpace.js'

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
// iframes swallow events and the drawer is inert.

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
// unrelated action during this short window. A fresh pointerdown is a new user
// interaction, not the old drag's compat click, so it retires any standing guard
// before that interaction can produce its own click.
function suppressNextSourceClick(sourceEl) {
  let cleared = false
  const clear = () => {
    if (cleared) return
    cleared = true
    window.removeEventListener('pointerdown', clear, true)
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
  window.addEventListener('pointerdown', clear, true)
  window.addEventListener('click', onClick, true)
  const timer = setTimeout(clear, 400)
  return clear
}

export default function useWorkspaceDrag({
  contentElRef,
  sceneInputsRef, // ref → { projection, mode, contentRect }
  workspaceStateRef, // ref → { ws, undo } (advanced synchronously by Shell's dispatch)
  dispatchWorkspace,
  labelForTabRef, // ref → (tab) => string
  dragActiveRef, // shared flag the Drawer's swipe-close handlers stand down on
  drawerOpenRef,
  drawerRowGesturesRef, // key -> ref({ openMenu, beginReorder }) registered by Drawer rows
  closeDrawer,
  openDrawer,
  openTabMenuAtRef, // ref -> (clientX, clientY, tab, paneId) => void
  onPreviewBuilder, // (active, { committed }) => void — enter/leave the LIVE
  // builder preview a single-mode drag unfolds (point 15: dragging IS
  // building). Render-only: the reducer viewMode stays 'single' until the drop
  // commits 'panes', so ONE undo reverts both the tree AND the mode. The leave
  // call carries the outcome for drawer restoration. Presentation does not
  // predict that outcome: OPEN_TAB_AT owns the durable mode and Shell mirrors
  // its actual result in the same batch. (Settings needs no conversion across the flip — its tab survives;
  // single mode paints its own slot, never a forced takeover.)
}) {
  useEffect(() => {
    // ── Reusable overlay DOM (created lazily on the first arm) ────────────────
    let shieldEl = null
    let chipEl = null
    let chipWidth = 0
    let previewEl = null
    let fixedSpace = null
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

    // ── Momentum fling for the JS-owned drawer scroll ─────────────────────────
    // Pinned drawer rows keep touch-action:pinch-zoom so a held pin can be dragged
    // to reorder, which means the browser never scrolls those rows natively — the
    // session pans .drawer__scroll 1:1 with the finger instead (onMove below).
    // Without a fling that 1:1 pan dead-stops the instant the finger lifts, so on a
    // tall pinned band the whole list reads as "stuck / something stops the scroll."
    // This carries the release velocity and decelerates on rAF, reproducing native
    // momentum. It lives at the effect scope so a glide outlives its pointer session
    // and a fresh press (or unmount) can halt it. Frame-rate independent: velocity
    // is layout px/ms and decays by exp(-FRICTION·dt), matching iOS-normal feel.
    let flingRAF = 0
    const FLING_FRICTION = 0.002 // per-ms velocity decay (≈ 0.998/ms, iOS normal)
    const FLING_MIN_V = 0.04 // px/ms — below this the glide has visually stopped
    function stopFling() {
      if (flingRAF) { cancelAnimationFrame(flingRAF); flingRAF = 0 }
    }
    function startFling(el, velocity) {
      stopFling()
      if (!el || !Number.isFinite(velocity) || Math.abs(velocity) < FLING_MIN_V) return
      let v = velocity
      let last = performance.now()
      const step = (now) => {
        flingRAF = 0
        const dt = Math.min(32, now - last) // clamp a long/background frame
        last = now
        const before = el.scrollTop
        el.scrollTop = before + v * dt
        // A short delta versus the request means the scroller hit top/bottom.
        if (Math.abs(el.scrollTop - before) + 0.5 < Math.abs(v * dt)) return
        v *= Math.exp(-FLING_FRICTION * dt)
        if (Math.abs(v) < FLING_MIN_V) return
        flingRAF = requestAnimationFrame(step)
      }
      flingRAF = requestAnimationFrame(step)
    }

    function contentBox() {
      return captureLayoutSpace(contentElRef.current)
    }
    function toLocal(clientX, clientY, box = contentBox()) {
      return clientPointToLayout({ x: clientX, y: clientY }, box)
    }

    function ensureOverlays() {
      fixedSpace = captureLayoutSpace(document.documentElement)
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
        document.body.appendChild(previewEl)
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
      chipEl?.remove(); chipEl = null; chipWidth = 0
      previewEl?.remove(); previewEl = null
      fixedSpace = null
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
        chipWidth = chipEl.offsetWidth || 0
      }
      const viewport = fixedSpace || captureLayoutSpace(document.documentElement)
      const point = clientPointToLayout({ x: clientX, y: clientY }, viewport)
      const { left, top } = chipOffset(point, isTouch)
      // V5 (vizreview): clamp the chip within the viewport so its label never clips
      // at the right edge (the +12 offset pushed a right-edge drag off-screen).
      const margin = 8
      const viewportWidth = viewport.width || window.innerWidth
      const maxLeft = Math.max(margin, viewportWidth - chipWidth - margin)
      chipEl.style.left = `${Math.max(margin, Math.min(left, maxLeft))}px`
      chipEl.style.top = `${top}px`
    }

    // Render (or clear) the drop preview for a zone. The geometry engine speaks
    // content-local pixels. Translate once to viewport coordinates so the fixed
    // preview can paint over both pane and strip chrome without clipping.
    function renderPreview(zone, box) {
      if (!previewEl) return
      if (!zone) { previewEl.classList.remove('is-visible'); return }
      previewEl.classList.toggle('workspace__drop-preview--caret', zone.type === 'strip')
      const { rect } = zone
      const viewport = fixedSpace || captureLayoutSpace(document.documentElement)
      const contentOrigin = clientPointToLayout({
        x: box.clientLeft,
        y: box.clientTop,
      }, viewport)
      const toViewportLength = value => clientLengthToLayout(
        value * box.zoom,
        viewport,
      )
      previewEl.style.left = `${contentOrigin.x + toViewportLength(rect.x)}px`
      previewEl.style.top = `${contentOrigin.y + toViewportLength(rect.y)}px`
      previewEl.style.width = `${toViewportLength(rect.w)}px`
      previewEl.style.height = `${toViewportLength(rect.h)}px`
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

    function findStrip(paneId) {
      const selector = `[data-pane-strip="${cssEscape(paneId)}"]`
      const shell = contentElRef.current?.closest?.('.shell')
        || contentElRef.current?.parentElement
      return shell?.querySelector?.(selector) || null
    }

    // Measure one pane's whole strip contract. The one-pane Builder strip is a
    // shell sibling aligned with the content top; tiled strips are content
    // children. Keeping rect + tabs together prevents hit-testing, previews, and
    // auto-scroll from inventing different ideas of where the strip lives.
    function measureStrip(paneId, box = contentBox()) {
      const strip = findStrip(paneId)
      if (!strip) return null
      const stripBox = strip.getBoundingClientRect()
      const origin = clientPointToLayout({ x: stripBox.left, y: stripBox.top }, box)
      return {
        rect: {
          x: origin.x,
          y: origin.y,
          w: clientLengthToLayout(stripBox.width, box),
          h: clientLengthToLayout(stripBox.height, box),
        },
        tabs: [...strip.querySelectorAll('.shell__tab')].map((el) => {
          const r = el.getBoundingClientRect()
          return {
            left: toLocal(r.left, r.top, box).x,
            right: toLocal(r.right, r.bottom, box).x,
          }
        }),
      }
    }

    function refreshSceneStrips(activeScene, box = contentBox()) {
      if (!activeScene) return
      for (const pane of activeScene.panes) {
        const measured = measureStrip(pane.paneId, box)
        if (!measured) continue
        pane.stripRect = measured.rect
        pane.tabs = measured.tabs
      }
    }

    function buildSceneNow(source, allowRootEdge) {
      const { projection, mode, contentRect } = sceneInputsRef.current
      const ws = workspaceStateRef.current.ws
      const box = contentBox()
      return buildScene(
        ws, projection, mode, contentRect, source, allowRootEdge,
        paneId => measureStrip(paneId, box),
      )
    }

    // ── One drag session ──────────────────────────────────────────────────────
    function startSession(downEvent, srcEl, sourceKind, key, paneId) {
      const isTouch = downEvent.pointerType !== 'mouse'
      const start = { x: downEvent.clientX, y: downEvent.clientY }
      const pointerId = downEvent.pointerId
      let armed = false
      let cancelled = false
      let cleaned = false
      let menuOpened = false
      let holdTimer = null
      let held = false
      let scrolling = false
      let scrollEl = null
      let scrollAxis = null
      let scrollSpace = null
      // Recent {t, top} samples so the lift-off velocity is a short trailing
      // AVERAGE, not just the final (decelerating) move — see flingReleaseVelocity.
      let scrollSamples = []
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
      const drawerGesture = () => drawerRowGesturesRef?.current?.get(key)?.current

      // iOS callout/selection suppression begins NOW (pointerdown), scoped to the
      // source, for the WHOLE hold window — not at arm, when the magnifier has
      // already won. Swallow touch `contextmenu` in capture phase so it cannot
      // bypass the shared hold timer through a tab or row's own handler. Mouse
      // right-click is untouched because this listener exists only for touch.
      let ctxListener = null
      if (isTouch) {
        prevBodySelect = document.body.style.userSelect
        document.body.style.userSelect = 'none'
        document.body.style.webkitUserSelect = 'none'
        srcEl.style.webkitTouchCallout = 'none'
        srcEl.style.userSelect = 'none'
        ctxListener = (ev) => {
          ev.preventDefault()
          ev.stopImmediatePropagation()
        }
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
          const drawer = document.getElementById('navigation-drawer')
          if (drawer) {
            const drawerSpace = captureLayoutSpace(drawer)
            drawerEdgeX = drawerSpace.clientLeft + drawerSpace.width * drawerSpace.zoom
          }
        }
      }

      // Tabs and drawer rows share this ONE pointer owner AND one two-stage
      // press-and-hold. After the first stage the row/tab becomes draggable, so
      // movement picks it up (reorder, workspace drag, or a strip drag); if the
      // pointer instead stays still through the second stage, that item's actions
      // open immediately, while still held. Movement clears the same timer before
      // handing off to a drag or to scrolling, so no competing gesture lifecycle
      // exists — a short hold moves and a long hold opens actions everywhere.
      if (isTouch) {
        holdTimer = setTimeout(() => {
          if (cancelled || cleaned) return
          held = true
          if (navigator.vibrate) { try { navigator.vibrate(8) } catch { /* unsupported */ } }
          holdTimer = setTimeout(() => {
            if (cancelled || cleaned || armed || scrolling) return
            const point = { ...lastPoint }
            // Keep this pointer session alive until the contact actually ends.
            // Restoring body selection here leaves a small window in which the
            // platform long-press can select whichever menu item appeared under
            // the still-held finger (notably Android's text-selection handles).
            if (sourceKind === 'drawer') {
              const handler = drawerGesture()
              if (!handler?.openMenu) {
                cleanup()
                return
              }
              menuOpened = true
              handler.openMenu(point)
            } else {
              const openTabMenu = openTabMenuAtRef?.current
              if (!openTabMenu) {
                cleanup()
                return
              }
              menuOpened = true
              openTabMenu(point.x, point.y, tabFromKey(key), paneId)
            }
          }, PRESS_MENU_HOLD_MS - PRESS_DRAG_HOLD_MS)
        }, PRESS_DRAG_HOLD_MS)
      }

      function stopAutoScroll() {
        if (autoRAF) { cancelAnimationFrame(autoRAF); autoRAF = null }
        autoDir = 0
        autoStripEl = null
        autoPaneId = null
      }
      // Strip auto-scroll (§3.2): near an overflowing strip's edge, scroll it
      // 6px/frame and re-measure so the caret keeps tracking under the pointer.
      function updateAutoScroll(clientX, clientY, box = contentBox()) {
        const { x: xL, y: yL } = toLocal(clientX, clientY, box)
        let stripEl = null
        let dir = 0
        let pid = null
        for (const pane of scene.panes) {
          const r = pane.stripRect
          if (xL < r.x || xL > r.x + r.w || yL < r.y || yL > r.y + r.h + STRIP_CARET_PAD) continue
          const el = findStrip(pane.paneId)
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
        const box = contentBox()
        const p = scene?.panes.find(pp => pp.paneId === autoPaneId)
        const measured = measureStrip(autoPaneId, box)
        if (p && measured) {
          p.stripRect = measured.rect
          p.tabs = measured.tabs
        }
        const next = hitTest(toLocal(lastPoint.x, lastPoint.y, box), scene, curZone)
        renderPreview(next, box)
        curZone = next
        autoRAF = requestAnimationFrame(autoStep)
      }

      // The per-frame drag work is rAF-coalesced and batches geometry reads before
      // style writes. A pointermove can fire several times per frame; one content
      // rect feeds both auto-scroll and hit-testing, while the chip width is
      // measured only once when the drag arms.
      let moveRAF = 0
      const doMoveWork = () => {
        moveRAF = 0
        if (!armed || cleaned) return
        const { x: cx, y: cy } = lastPoint
        // While the drawer still covers the panes, show no preview (the glide-close
        // crossing is handled synchronously in onMove).
        if (sourceKind === 'drawer' && !glided) {
          positionChip(cx, cy, isTouch, key)
          curZone = null; renderPreview(null); stopAutoScroll(); return
        }
        const box = contentBox()
        // Arming a single-mode drag changes the rendered strip owner. Refresh
        // after that React commit (and after any subsequent layout change) so
        // hit-testing follows the strip that is actually mounted this frame.
        refreshSceneStrips(scene, box)
        updateAutoScroll(cx, cy, box)
        const next = hitTest(toLocal(cx, cy, box), scene, curZone)
        positionChip(cx, cy, isTouch, key)
        renderPreview(next, box)
        curZone = next
      }
      const onMove = (ev) => {
        if (ev.pointerId !== pointerId) return // ignore a second finger
        if (menuOpened) {
          ev.preventDefault?.()
          return
        }
        const previousPoint = lastPoint
        lastPoint = { x: ev.clientX, y: ev.clientY }
        const dx = ev.clientX - start.x
        const dy = ev.clientY - start.y
        if (scrolling) {
          ev.preventDefault?.()
          if (scrollEl && scrollAxis === 'x') {
            scrollEl.scrollLeft += clientLengthToLayout(
              previousPoint.x - ev.clientX,
              scrollSpace,
            )
          } else if (scrollEl && scrollAxis === 'y') {
            scrollEl.scrollTop += clientLengthToLayout(
              previousPoint.y - ev.clientY,
              scrollSpace,
            )
            // Sample the real scroll position; the lift handler turns the last
            // ~110ms of these into a release velocity for the glide.
            const now = performance.now()
            scrollSamples.push({ t: now, top: scrollEl.scrollTop })
            while (scrollSamples.length > 2 && now - scrollSamples[0].t > 110) {
              scrollSamples.shift()
            }
          }
          return
        }
        if (!armed) {
          if (sourceKind === 'drawer') {
            const intent = drawerRowMoveIntent(dx, dy, {
              held,
              isTouch,
              pinned: srcEl.hasAttribute('data-pinned-key'),
            })
            if (intent === 'pending') return
            if (intent === 'scroll') {
              clearTimeout(holdTimer)
              scrolling = true
              scrollEl = srcEl.closest('.drawer__scroll')
              scrollAxis = 'y'
              scrollSamples = []
              ev.preventDefault?.()
              if (scrollEl) {
                scrollSpace = captureLayoutSpace(scrollEl)
                scrollEl.scrollTop += clientLengthToLayout(
                  start.y - ev.clientY,
                  scrollSpace,
                )
                scrollSamples.push({ t: performance.now(), top: scrollEl.scrollTop })
              }
              return
            }
            if (intent === 'reorder') {
              const handler = drawerGesture()
              ev.preventDefault?.()
              cancelled = true
              cleanup()
              handler?.beginReorder?.({ pointerId, start, moveEvent: ev })
              return
            }
            if (intent === 'workspace') arm()
            else {
              cancelled = true
              cleanup()
            }
            if (!armed) return
          } else if (isTouch) {
            if (held && passedSlop(dx, dy)) arm()
            if (armed) {
              // Continue below so the move that first proves drag intent also
              // positions the chip and preview; no second move is required.
            } else if (held) {
              return
            } else {
              const intent = touchTabMoveIntent(dx, dy)
              if (intent === 'scroll') {
                clearTimeout(holdTimer)
                // The tab reserves one-finger panning so the browser cannot
                // reclaim a post-hold horizontal move and cancel a live drag.
                // Until the hold wins, mirror the strip's one-to-one pan here.
                // Whitespace and close buttons remain native pan-x.
                scrolling = true
                scrollEl = srcEl.closest('.shell__tabstrip')
                scrollAxis = 'x'
                ev.preventDefault?.()
                if (scrollEl) {
                  scrollSpace = captureLayoutSpace(scrollEl)
                  scrollEl.scrollLeft += clientLengthToLayout(
                    start.x - ev.clientX,
                    scrollSpace,
                  )
                }
                return
              }
              if (!armed) return
            }
          } else {
            if (passedSlop(dx, dy)) arm()
            if (!armed) return
          }
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
        if (menuOpened) {
          cleanup({ suppressClick: true })
          return
        }
        if (!armed) {
          if (scrolling) {
            // Turn the last ~110ms of travel into a release velocity and hand a
            // still-moving lift to the momentum glide. A finger that paused before
            // lifting leaves no fresh samples, so it keeps its exact rest position.
            if (scrollEl && scrollAxis === 'y') {
              startFling(scrollEl, flingReleaseVelocity(scrollSamples, performance.now()))
            }
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
          // An armed drag lifted essentially in place is a cancel, not a drop —
          // and never a menu: actions open from the hold timer while still held.
          cleanup({ suppressClick: true })
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
      const onCancel = (ev) => {
        if (ev.pointerId === pointerId) cleanup({ suppressClick: menuOpened || armed || scrolling })
      }
      const onKey = (ev) => { if (ev.key === 'Escape' && armed) { ev.preventDefault(); cleanup({ suppressClick: true }) } }
      // Touch pointers already have implicit capture, and Chromium may release and
      // reacquire it while the strip updates without ending the contact. The window
      // listeners still receive that stream, so only pointerup/pointercancel is a
      // terminal touch signal. A mouse capture loss remains a real cancellation.
      const onLostCapture = (ev) => {
        if (ev.pointerId === pointerId && !isTouch) cleanup({ suppressClick: armed })
      }
      const onWinBlur = () => cleanup({ suppressClick: menuOpened || armed || scrolling })
      const onVisibility = () => {
        if (document.visibilityState === 'hidden') cleanup({ suppressClick: menuOpened || armed || scrolling })
      }
      // BFCache freeze / bfcache navigation can be the ONLY interruption event some
      // browsers fire — no pointercancel, no blur, and (on older Safari) no
      // visibilitychange-hidden first. Without this, a drag frozen mid-flight and
      // then restored would keep its render-only builder preview, wedging the
      // workspace tiled. pagehide cancels the drag as the page is frozen/unloaded.
      const onPageHide = () => cleanup({ suppressClick: menuOpened || armed || scrolling })

      function cleanup({ suppressClick = false, committed = false } = {}) {
        if (cleaned) return
        cleaned = true
        // Leave the live builder preview. This callback can retire transient
        // preview state only. On a committed drop OPEN_TAB_AT has already
        // changed the workspace and Shell's actual-transition synchronizer has
        // committed presentation; on cancel the workspace never changed and
        // the preview simply folds away.
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
      // Touching the list halts an in-flight momentum glide, the way native
      // scrolling stops under a finger.
      stopFling()
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
      stopFling() // no rAF may outlive the effect
      clearPendingSourceClick?.()
      removeOverlays()
    }
    // Every volatile input arrives through a ref, so the listener installs once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
}
