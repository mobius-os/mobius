/* Drawer Apps card: every installed app as one row of icons that expands to show them all. */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ChevronDown, Grid } from '@openai/apps-sdk-ui/components/Icon'
import AppIcon from '../AppIcon.jsx'
import InlineRenameInput from './InlineRenameInput.jsx'
import useItemMenuGestures, { itemMenuPlacement } from './useItemMenuGestures.js'
import { drawerNameMaxLength } from './drawerRename.js'
import { appTileLabel, reconcileStripOrder, wheelRowScrollDelta } from './drawerInformationArchitecture.js'
import { PRESS_DRAG_HOLD_MS } from '../Shell/dragController.js'

const STORAGE_KEY = 'mobius.drawer.appsExpanded'
const ORDER_KEY = 'mobius.drawer.appsOrder'
const TILE = 48
// Fixed spacing between icons; it never stretches with the sidebar width.
const GAP = 10

function readOrder() {
  try {
    const saved = JSON.parse(localStorage.getItem(ORDER_KEY) || 'null')
    return Array.isArray(saved) ? saved.map(String) : null
  } catch {
    return null
  }
}

function readExpanded() {
  try { return localStorage.getItem(STORAGE_KEY) === '1' } catch { return false }
}

// The gesture registry key for a strip tile. A pinned app also has a Pinned
// row under `app:<id>`, so the tile registers its own key and carries it as
// `data-drawer-key`; the drag key stays `app:<id>` so a drop opens the app.
export function stripTileGestureKey(appId) {
  return `apps-strip:${appId}`
}

// One app icon. Touch press-and-hold, mouse drag into the workspace, and the
// held click all belong to the shell's shared drag controller (the same one
// every sidebar row uses), found through `data-drag-key`. Right-click and the
// keyboard menu keys come from the shared item-menu gestures.
function DrawerAppTile({
  app,
  active,
  needsYou,
  building,
  fresh,
  actions,
  drawerRowGesturesRef,
  onOpen,
  showTip,
  hideTip,
}) {
  const name = app.name || 'App'
  const buttonRef = useRef(null)
  function openMenuAt(point, trigger = buttonRef.current, { focusFirstAction = false } = {}) {
    actions?.openMenu({
      kind: 'app',
      id: app.id,
      surface: 'strip',
      placement: itemMenuPlacement(point, buttonRef.current),
      focusFirstAction,
      restoreFocusTarget: trigger,
    })
  }
  const { openItemMenu, openMenuFromKey, recordItemMenuPointer, beginSecondaryMenuPress } = useItemMenuGestures(openMenuAt)

  const gestureHandlerRef = useRef(null)
  gestureHandlerRef.current = {
    openMenu: point => openMenuAt(point, buttonRef.current),
  }
  const gestureKey = stripTileGestureKey(app.id)
  useLayoutEffect(() => {
    const registry = drawerRowGesturesRef?.current
    if (!registry) return undefined
    registry.set(gestureKey, gestureHandlerRef)
    return () => {
      if (registry.get(gestureKey) === gestureHandlerRef) registry.delete(gestureKey)
    }
  }, [drawerRowGesturesRef, gestureKey])

  return (
    <button
      ref={buttonRef}
      type="button"
      className={`drawer__apps-card-tile${active ? ' drawer__apps-card-tile--active' : ''}`}
      aria-label={appTileLabel(name, { needsYou, fresh, building })}
      aria-current={active ? 'page' : undefined}
      data-drag-key={`app:${app.id}`}
      data-drawer-key={gestureKey}
      onMouseEnter={(event) => showTip(event.currentTarget, name)}
      onMouseLeave={hideTip}
      onFocus={(event) => {
        if (event.currentTarget.matches(':focus-visible')) showTip(event.currentTarget, name)
      }}
      onBlur={hideTip}
      onPointerDown={(event) => {
        recordItemMenuPointer(event)
        beginSecondaryMenuPress(event)
      }}
      onClick={() => {
        hideTip()
        onOpen(app)
      }}
      onContextMenu={openItemMenu}
      onKeyDown={openMenuFromKey}
    >
      <AppIcon item={app} label={name} className="drawer__apps-card-icon" />
      {needsYou && <span className="drawer__apps-card-dot drawer__apps-card-dot--needs" aria-hidden="true" />}
      {!needsYou && fresh && <span className="drawer__apps-card-dot drawer__apps-card-dot--new" aria-hidden="true" />}
      {building && <span className="drawer__apps-card-dot drawer__apps-card-dot--building" aria-hidden="true" />}
    </button>
  )
}

export default function DrawerAppsStrip({
  apps: recentApps,
  activeAppId,
  ownerInputSet,
  streamingSet,
  newAppSet,
  onApp,
  actions,
  drawerRowGesturesRef,
  suppressReleaseClick,
  titleRef,
  appsViewActive,
  onOpenAll,
  renamingId,
  onLayoutChange,
}) {
  const [expanded, setExpanded] = useState(readExpanded)
  // How many icons show without scrolling; set by the width measurement below.
  const [visibleCount, setVisibleCount] = useState(5)
  const recentIds = recentApps.map(app => String(app.id))
  const recentKey = recentIds.join(',')
  const [order, setOrder] = useState(() => reconcileStripOrder(readOrder() || recentIds, recentIds, null, 0))
  const newestRef = useRef(recentIds[0] ?? null)
  useEffect(() => {
    const newest = recentIds[0] ?? null
    // The first load is not an open. An app opened elsewhere (another surface
    // or device) becomes the newest and is promoted here; a tap on the row
    // promotes directly in openApp.
    const promote = newestRef.current != null && newest !== newestRef.current ? newest : null
    newestRef.current = newest
    setOrder(current => {
      const next = reconcileStripOrder(current, recentIds, promote, visibleCount)
      return next.join(',') === current.join(',') ? current : next
    })
  // recentKey captures recentIds' content; visibleCount is read at change time.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recentKey])
  useEffect(() => {
    try { localStorage.setItem(ORDER_KEY, JSON.stringify(order)) } catch { /* private mode */ }
  }, [order])
  const byId = new Map(recentApps.map(app => [String(app.id), app]))
  const apps = order.map(id => byId.get(id)).filter(Boolean)
  const [perRow, setPerRow] = useState(6)
  const [rowWidth, setRowWidth] = useState(null)
  const rowRef = useRef(null)
  // Which edges of the collapsed, horizontally scrolling row have more apps
  // beyond them; those edges fade out to show the row scrolls.
  const [edges, setEdges] = useState({ start: false, end: false })
  const updateEdges = useCallback(() => {
    const row = rowRef.current
    if (!row) return
    const start = row.scrollLeft > 6
    const end = row.scrollLeft + row.clientWidth < row.scrollWidth - 6
    setEdges(current => (current.start === start && current.end === end ? current : { start, end }))
  }, [])
  const hasApps = apps.length > 0
  // App name label: positioned in the section from the icon's rectangle and
  // nudged to stay inside the sidebar.
  const sectionRef = useRef(null)
  const tipRef = useRef(null)
  const [tip, setTip] = useState(null)
  function showTip(target, name) {
    // Layout units (offset*), not screen rectangles: the drawer can be scaled,
    // and screen pixels would skew the label off-centre. The icon's list item
    // is positioned relative to this section; subtract the row's scroll.
    const item = target.closest('li')
    const row = rowRef.current
    if (!item || !row) return
    setTip({
      name,
      center: item.offsetLeft - row.scrollLeft + item.offsetWidth / 2,
      top: item.offsetTop - row.scrollTop - 6,
    })
  }
  const hideTip = useCallback(() => setTip(null), [])
  useLayoutEffect(() => {
    const el = tipRef.current
    const section = sectionRef.current
    if (!el || !section || !tip) return
    const width = el.offsetWidth
    const max = section.clientWidth - width
    el.style.left = `${Math.max(0, Math.min(max, tip.center - width / 2))}px`
    el.style.top = `${tip.top - el.offsetHeight}px`
  }, [tip])

  // Finger drag for the collapsed row. iOS Safari will not natively pan a
  // sideways scroller inside the drawer (whose own touch handling reserves
  // horizontal swipes), so the row follows the finger itself and glides on
  // release. Where the browser does take over natively it sends
  // pointercancel and this stands down. A press held past the shared drag
  // stage belongs to the drag controller (lift into the workspace, or the
  // menu), so the row only pans for a swipe that starts before it.
  const dragRef = useRef(null)
  const glideRef = useRef(0)
  function stopGlide() {
    cancelAnimationFrame(glideRef.current)
    glideRef.current = 0
  }
  function onRowPointerDown(event) {
    if (expanded || event.pointerType === 'mouse') return
    const row = rowRef.current
    if (!row) return
    stopGlide()
    dragRef.current = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      downAt: performance.now(),
      left: row.scrollLeft,
      moving: false,
      lastX: event.clientX,
      lastT: performance.now(),
      velocity: 0,
    }
  }
  function onRowPointerMove(event) {
    const drag = dragRef.current
    const row = rowRef.current
    if (!drag || !row || event.pointerId !== drag.id) return
    const dx = event.clientX - drag.x
    const dy = event.clientY - drag.y
    if (!drag.moving) {
      if (Math.abs(dx) < 6 || Math.abs(dx) < Math.abs(dy)) {
        if (Math.abs(dy) > 10) dragRef.current = null
        return
      }
      if (performance.now() - drag.downAt >= PRESS_DRAG_HOLD_MS) {
        dragRef.current = null
        return
      }
      drag.moving = true
    }
    const now = performance.now()
    const dt = Math.max(1, now - drag.lastT)
    drag.velocity = 0.8 * ((event.clientX - drag.lastX) / dt) + 0.2 * drag.velocity
    drag.lastX = event.clientX
    drag.lastT = now
    row.scrollLeft = drag.left - dx
  }
  function onRowPointerEnd(event) {
    const drag = dragRef.current
    const row = rowRef.current
    if (!drag || event.pointerId !== drag.id) return
    dragRef.current = null
    if (!drag.moving || !row) return
    if (event.type === 'pointercancel') return
    // The lifted finger must not also open the app under it. The drawer's
    // one-shot suppressor swallows only that touch click; any new press or
    // key retires it.
    suppressReleaseClick?.()
    let velocity = -drag.velocity * 16 // px per frame
    const step = () => {
      velocity *= 0.94
      if (Math.abs(velocity) < 0.5) {
        glideRef.current = 0
        return
      }
      row.scrollLeft += velocity
      glideRef.current = requestAnimationFrame(step)
    }
    glideRef.current = requestAnimationFrame(step)
  }
  useEffect(() => stopGlide, [])

  // A mouse wheel scrolls the collapsed row sideways while the row can still
  // move that way; at either end the wheel scrolls the sidebar as usual.
  // Registered natively because React's wheel listener is passive.
  useEffect(() => {
    const row = rowRef.current
    if (!row || expanded) return undefined
    const onWheel = (event) => {
      const delta = wheelRowScrollDelta(event, row)
      if (!delta) return
      event.preventDefault()
      stopGlide()
      row.scrollLeft += delta
    }
    row.addEventListener('wheel', onWheel, { passive: false })
    return () => row.removeEventListener('wheel', onWheel)
  }, [expanded, hasApps])

  // Opening an app found by scrolling moves it to the front at once, and the
  // row returns to its start to show it there. Scroll again once the reordered
  // list has rendered (a DOM reorder can cancel a smooth scroll mid-way,
  // notably in Safari), and then settle exactly at the start. An app already
  // in view keeps its place.
  const openedAppRef = useRef(null)
  const settleRef = useRef(0)
  function scrollRowToStart() {
    const row = rowRef.current
    if (!row) return
    stopGlide()
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    row.scrollTo({ left: 0, behavior: reduce ? 'auto' : 'smooth' })
    clearTimeout(settleRef.current)
    settleRef.current = setTimeout(() => {
      if (rowRef.current && !dragRef.current) rowRef.current.scrollLeft = 0
    }, 500)
  }
  useLayoutEffect(() => {
    const opened = openedAppRef.current
    if (opened == null || String(apps[0]?.id) !== String(opened)) return
    openedAppRef.current = null
    scrollRowToStart()
  })
  useEffect(() => () => clearTimeout(settleRef.current), [])
  function openApp(app, index) {
    if (!expanded && index >= visibleCount) {
      const id = String(app.id)
      openedAppRef.current = id
      setOrder(current => reconcileStripOrder(current, recentIds, id, visibleCount))
    }
    onApp?.(app.id)
  }

  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, expanded ? '1' : '0') } catch { /* private mode */ }
  }, [expanded])

  // How many tiles fit on one line: the expanded grid's column count, and
  // whether the row overflows at all (which shows Show all).
  useLayoutEffect(() => {
    const row = rowRef.current
    const host = row?.parentElement
    if (!row || !host) return undefined
    const measure = () => {
      // Measure the section (not the row, whose width we set below); the row's
      // 4px side padding is not room for a tile.
      const width = host.clientWidth - 8
      const fit = Math.max(2, Math.floor((width + GAP) / (TILE + GAP)))
      setPerRow(fit)
      // The collapsed row grows and shrinks in whole-icon steps, always ending
      // half-way through the next icon, so resizing the sidebar never slides a
      // partial icon along the faded edge.
      const full = Math.max(1, Math.floor((width - TILE / 2) / (TILE + GAP)))
      setRowWidth(full * (TILE + GAP) + TILE / 2 + 8)
      setVisibleCount(full)
      updateEdges()
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(host)
    return () => observer.disconnect()
  }, [hasApps, updateEdges])

  // Any change to this card's height (expanding, the rename field, wrapping
  // to more grid rows on resize) moves the windowed Recents list below it, so
  // the drawer re-measures whenever the card's height actually changes.
  useLayoutEffect(() => {
    const section = sectionRef.current
    if (!section || !onLayoutChange) return undefined
    let height = section.offsetHeight
    onLayoutChange()
    const observer = new ResizeObserver(() => {
      const next = section.offsetHeight
      if (next === height) return
      height = next
      onLayoutChange()
    })
    observer.observe(section)
    return () => observer.disconnect()
  }, [onLayoutChange])
  useLayoutEffect(() => { updateEdges() }, [expanded, apps.length, perRow, updateEdges])

  const renamingApp = renamingId != null
    ? apps.find(app => String(app.id) === String(renamingId))
    : null

  return (
    <section ref={sectionRef} className="drawer__apps-card" aria-labelledby="drawer-apps-label">
      <div className="drawer__apps-card-header">
        <h2 id="drawer-apps-label" className="drawer__apps-card-title">
          {/* The heading is the sidebar's way into the full Apps screen
              (search, every installed app, install and share). */}
          <button
            ref={titleRef}
            type="button"
            className={`drawer__apps-card-open${appsViewActive ? ' drawer__apps-card-open--active' : ''}`}
            aria-current={appsViewActive ? 'page' : undefined}
            aria-label={`Apps, ${apps.length} installed. Open all apps`}
            onClick={onOpenAll}
          >
            <Grid className="drawer__section-icon" width={16} height={16} aria-hidden="true" />
            Apps <span className="drawer__apps-card-count">{apps.length}</span>
          </button>
        </h2>
        {apps.length > perRow && (
          <button
            type="button"
            className="drawer__apps-card-toggle"
            aria-expanded={expanded}
            onClick={() => setExpanded(value => !value)}
          >
            {expanded ? 'Show less' : 'Show all'}
            <ChevronDown
              className={`drawer__apps-card-chevron${expanded ? ' drawer__apps-card-chevron--open' : ''}`}
              width={14}
              height={14}
              aria-hidden="true"
            />
          </button>
        )}
      </div>
      {renamingApp && (
        <div key={renamingApp.id} className="drawer__apps-card-rename">
          <AppIcon item={renamingApp} label={renamingApp.name || 'App'} className="drawer__apps-card-icon" />
          <InlineRenameInput
            className="drawer__rename-input"
            initialValue={renamingApp.name || ''}
            maxLength={drawerNameMaxLength('app')}
            ariaLabel="Rename app"
            onSubmit={next => actions?.submitRename('app', renamingApp.id, renamingApp.name, next)}
            onCancel={() => actions?.cancelRename()}
          />
        </div>
      )}
      {apps.length > 0 && <ul
        ref={rowRef}
        className={[
          'drawer__apps-card-grid',
          expanded ? 'drawer__apps-card-grid--expanded' : 'drawer__apps-card-grid--row',
          !expanded && edges.start ? 'drawer__apps-card-grid--fade-start' : '',
          !expanded && edges.end ? 'drawer__apps-card-grid--fade-end' : '',
        ].filter(Boolean).join(' ')}
        style={!expanded && rowWidth ? { width: `${rowWidth}px` } : undefined}
        onScroll={expanded ? undefined : () => { hideTip(); updateEdges() }}
        data-drawer-hscroll={expanded ? undefined : ''}
        onPointerDown={onRowPointerDown}
        onPointerMove={onRowPointerMove}
        onPointerUp={onRowPointerEnd}
        onPointerCancel={onRowPointerEnd}
      >
        {apps.map((app, index) => (
          <li key={app.id}>
            <DrawerAppTile
              app={app}
              active={activeAppId != null && Number(activeAppId) === Number(app.id)}
              needsYou={!!(app.chat_id && ownerInputSet.has(app.chat_id))}
              building={!!(app.chat_id && streamingSet.has(app.chat_id))}
              fresh={newAppSet.has(Number(app.id))}
              actions={actions}
              drawerRowGesturesRef={drawerRowGesturesRef}
              onOpen={target => openApp(target, index)}
              showTip={showTip}
              hideTip={hideTip}
            />
          </li>
        ))}
      </ul>}
      {tip && (
        <div ref={tipRef} className="drawer__apps-tip" role="tooltip">{tip.name}</div>
      )}
    </section>
  )
}
