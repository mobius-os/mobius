import { useCallback, useEffect, useRef } from 'react'
import * as tabModel from './tabModel.js'

export const WORKSPACE_SHORTCUT_CAPABILITY = 'workspace.shortcuts'

export function hasWorkspaceShortcutProvider(apps) {
  return Array.isArray(apps) && apps.some(app => (
    app?.capability_contract?.runtime?.[WORKSPACE_SHORTCUT_CAPABILITY]?.version === 1
  ))
}

function isApplePlatform(platform) {
  return /Mac|iPhone|iPad|iPod/i.test(String(platform || ''))
}

export function workspaceShortcutAction(event, platform = globalThis.navigator?.platform || '') {
  if (!event || isApplePlatform(platform) || event.isComposing || event.repeat) return null
  if (!event.ctrlKey || !event.altKey || event.metaKey) return null
  if (event.getModifierState?.('AltGraph')) return null

  const key = String(event.key || '')
  const lower = key.toLowerCase()
  if (lower === 't') return event.shiftKey ? 'restore' : 'open'
  if (lower === 'w' && !event.shiftKey) return 'close'
  if (event.shiftKey) return null
  if (key === 'PageDown') return 'next'
  if (key === 'PageUp') return 'previous'
  if (/^[1-9]$/.test(key)) return `select:${key}`
  return null
}

function editableElement(element) {
  if (!element || typeof element !== 'object') return false
  if (element.isContentEditable) return true
  return typeof element.closest === 'function'
    && !!element.closest('input, textarea, select, [contenteditable], [role="textbox"]')
}

export default function useWorkspaceShortcuts({
  enabled,
  workspaceStateRef,
  dispatchWorkspace,
  startNewChat,
}) {
  const actionsRef = useRef({ workspaceStateRef, dispatchWorkspace, startNewChat })
  actionsRef.current = { workspaceStateRef, dispatchWorkspace, startNewChat }

  const execute = useCallback((event, { forwarded = false } = {}) => {
    if (!enabled) return false
    if (forwarded ? event.editable : editableElement(document.activeElement)) return false
    const action = workspaceShortcutAction(event)
    if (!action) return false

    const { workspaceStateRef: stateRef, dispatchWorkspace: dispatch, startNewChat: openChat } = actionsRef.current
    const ws = stateRef.current.ws
    const pane = ws.panes[ws.focusedPaneId]
    let handled = false

    if (action === 'open') {
      handled = true
      openChat?.()
    } else if (action === 'restore') {
      handled = dispatch({ type: 'RESTORE_CLOSED_TAB' }) !== false
    } else if (ws.viewMode === 'panes' && pane) {
      if (action === 'close' && pane.activeTabKey) {
        handled = true
        dispatch({ type: 'CLOSE_TAB', tabKey: pane.activeTabKey, label: 'Closed tab' })
      } else if ((action === 'next' || action === 'previous') && pane.tabs.length > 1) {
        const current = Math.max(0, pane.tabs.findIndex(tab => tabModel.tabKey(tab) === pane.activeTabKey))
        const step = action === 'next' ? 1 : -1
        const target = pane.tabs[(current + step + pane.tabs.length) % pane.tabs.length]
        handled = true
        dispatch({ type: 'SET_ACTIVE', paneId: pane.id, tabKey: tabModel.tabKey(target) })
      } else if (action.startsWith('select:')) {
        const number = Number(action.slice(7))
        const index = number === 9 ? pane.tabs.length - 1 : number - 1
        const target = pane.tabs[index]
        if (target) {
          handled = true
          dispatch({ type: 'SET_ACTIVE', paneId: pane.id, tabKey: tabModel.tabKey(target) })
        }
      }
    }

    if (handled) event.preventDefault?.()
    return handled
  }, [enabled])

  useEffect(() => {
    const onKeyDown = event => execute(event)
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [execute])

  return useCallback(payload => execute(payload, { forwarded: true }), [execute])
}
