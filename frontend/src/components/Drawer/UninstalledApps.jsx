import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQueryClient } from '@tanstack/react-query'
import { Clock } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import { placeUnderAnchor } from '../../lib/contextMenuGeometry.js'
import { captureLayoutSpace, clientPointToLayout } from '../../lib/layoutSpace.js'
import AppIcon from '../AppIcon.jsx'
import { failureMessage, removedLabel } from './uninstalledApps.js'
import './UninstalledApps.css'

const ACTIONS = {
  recover: { call: id => api.apps.recover(id), working: 'Recovering…', failed: 'Could not recover this app. Try again.' },
  delete: { call: id => api.apps.deletePermanently(id), working: 'Deleting…', failed: 'Could not delete this app. Try again.' },
}

function iconItem(row) {
  return {
    id: row.id,
    slug: row.slug,
    icon_url: row.icon_data_url,
    background_color: row.background_color,
    theme_color: row.theme_color,
  }
}

// What can be done with an uninstalled app, anchored under its tile.
function UninstalledPopover({ row, countdown, anchorRef, focusFirst, working, error, onRun, onClose }) {
  const popoverRef = useRef(null)
  const [position, setPosition] = useState(null)

  useLayoutEffect(() => {
    const anchor = anchorRef.current
    const popover = popoverRef.current
    if (!anchor || !popover) return
    const space = captureLayoutSpace(document.documentElement)
    const box = element => {
      const rect = element.getBoundingClientRect()
      const topLeft = clientPointToLayout({ x: rect.left, y: rect.top }, space)
      const bottomRight = clientPointToLayout({ x: rect.right, y: rect.bottom }, space)
      return { left: topLeft.x, right: bottomRight.x, top: topLeft.y, bottom: bottomRight.y }
    }
    // The popover lines up with the grid's content edges horizontally, and stays inside the pane vertically.
    const grid = box(anchor.closest('.apps-directory__grid'))
    const pane = box(anchor.closest('.apps-directory'))
    setPosition(placeUnderAnchor({
      anchor: box(anchor),
      // Clear of the tile's focus ring and of the pointer that aims at it.
      gap: 14,
      padding: 0,
      bounds: { left: grid.left, right: grid.right, top: pane.top + 12, bottom: pane.bottom - 12 },
      size: { width: popover.offsetWidth, height: popover.offsetHeight },
    }))
  }, [anchorRef, error])

  // Keyboard users land on the first action; a pointer-opened popover stays visually neutral, like the item menu.
  useEffect(() => {
    if (focusFirst) popoverRef.current?.querySelector('button')?.focus()
  }, [focusFirst])

  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    const close = () => closeRef.current()
    function dismissOutside(event) {
      if (popoverRef.current?.contains(event.target) || anchorRef.current?.contains(event.target)) return
      close()
    }
    function dismissOnKey(event) {
      if (event.key !== 'Escape') return
      event.stopPropagation()
      close()
      anchorRef.current?.focus()
    }
    // The popover is fixed to the viewport, so scrolling the grid would leave it behind.
    document.addEventListener('pointerdown', dismissOutside, true)
    document.addEventListener('keydown', dismissOnKey, true)
    document.addEventListener('scroll', close, true)
    return () => {
      document.removeEventListener('pointerdown', dismissOutside, true)
      document.removeEventListener('keydown', dismissOnKey, true)
      document.removeEventListener('scroll', close, true)
    }
  }, [anchorRef])

  return createPortal(
    <div className="uninstalled-popover-layer">
      <div
        ref={popoverRef}
        className="uninstalled-popover"
        role="dialog"
        aria-label={`${row.name} actions`}
        data-positioned={position ? 'true' : 'false'}
        data-above={position?.above ? 'true' : 'false'}
        style={{
          '--popover-x': `${position?.x || 0}px`,
          '--popover-y': `${position?.y || 0}px`,
          '--popover-arrow': `${position?.arrow || 0}px`,
        }}
      >
        <div className="uninstalled-popover__who">
          <AppIcon item={iconItem(row)} label={row.name} size={null} className="uninstalled-popover__icon" />
          <div>
            <strong>{row.name}</strong>
            <span>{removedLabel(row.deleted_at)}</span>
          </div>
        </div>
        <p>
          <span className={`uninstalled-popover__pill${countdown.tone ? ` is-${countdown.tone}` : ''}`}>
            {countdown.label}
          </span>
          to recover
        </p>
        {error && <p className="uninstalled-popover__error" role="alert">{error}</p>}
        <div className="uninstalled-popover__actions">
          <button
            type="button"
            className="uninstalled-popover__btn uninstalled-popover__btn--primary"
            disabled={working !== null}
            onClick={() => onRun('recover')}
          >
            {working === 'recover' ? ACTIONS.recover.working : 'Recover'}
          </button>
          <button
            type="button"
            className="uninstalled-popover__btn uninstalled-popover__btn--danger"
            disabled={working !== null}
            onClick={() => onRun('delete')}
          >
            {working === 'delete' ? ACTIONS.delete.working : 'Delete permanently'}
          </button>
        </div>
        <p className="uninstalled-popover__note">
          Möbius keeps an uninstalled app’s data for 7 days.
        </p>
      </div>
    </div>,
    document.body,
  )
}

function UninstalledTile({ row, countdown, open, onToggle, onShow, onHide }) {
  const queryClient = useQueryClient()
  const buttonRef = useRef(null)
  // The request belongs to the tile, not the card: dismissing the card must not forget a request in flight,
  // lose its failure, or hand back fresh buttons that could start a second one.
  const [working, setWorking] = useState(null)
  const [error, setError] = useState('')

  async function run(kind) {
    setWorking(kind)
    setError('')
    try {
      const response = await ACTIONS[kind].call(row.id)
      if (!response.ok) {
        setError(await failureMessage(response, ACTIONS[kind].failed))
        onShow(row.id)
        return
      }
      onHide(row.id)
      await Promise.all([
        appQueries.list.invalidate(queryClient),
        appQueries.uninstalled.invalidate(queryClient),
      ])
    } catch {
      setError('Could not reach Möbius. Try again.')
      onShow(row.id)
    } finally {
      setWorking(null)
    }
  }

  return (
    <div className="apps-directory__card apps-directory__card--uninstalled">
      <button
        ref={buttonRef}
        type="button"
        className="apps-directory__card-main"
        aria-haspopup="dialog"
        aria-expanded={Boolean(open)}
        aria-label={`${row.name}, uninstalled, ${countdown.label}. Show actions`}
        title={`${row.name} · ${removedLabel(row.deleted_at)} · ${countdown.label}`}
        onClick={event => onToggle(row.id, event.detail === 0)}
      >
        <span className="uninstalled-apps__icon">
          <AppIcon item={iconItem(row)} label={row.name} size={null} className="apps-directory__card-icon" />
          <Clock className="uninstalled-apps__clock" data-tone={countdown.tone || undefined} aria-hidden="true" />
        </span>
        <span className="apps-directory__card-meta">
          <span className="apps-directory__card-name">{row.name}</span>
        </span>
      </button>
      {open && (
        <UninstalledPopover
          row={row}
          countdown={countdown}
          anchorRef={buttonRef}
          focusFirst={open.focusFirst}
          working={working}
          error={error}
          onRun={run}
          onClose={() => onHide(row.id)}
        />
      )}
    </div>
  )
}

// Uninstalled apps still inside their recovery window, as grayed tiles in the apps grid. Opening one shows
// what can be done with it.
export default function UninstalledTiles({ entries }) {
  const [open, setOpen] = useState(null)

  function toggle(id, focusFirst = false) {
    setOpen(current => (current?.id === id ? null : { id, focusFirst }))
  }
  // Only the card that asked is closed, so a late result cannot close a different tile's card.
  function hide(id) {
    setOpen(current => (current?.id === id ? null : current))
  }
  // Bring a card back to the front, e.g. to show a failure that arrived after it was dismissed.
  function show(id) {
    setOpen(current => (current?.id === id ? current : { id, focusFirst: false }))
  }

  return entries.map(({ row, countdown }) => (
    <UninstalledTile
      key={row.id}
      row={row}
      countdown={countdown}
      open={open?.id === row.id ? open : null}
      onToggle={toggle}
      onShow={show}
      onHide={hide}
    />
  ))
}
