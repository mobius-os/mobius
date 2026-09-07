import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  SHORTCUT_OVERRIDES_CHANGED_EVENT,
  SHORTCUT_OVERRIDES_STORAGE_KEY,
  findShellShortcut,
  frameShortcutBindings,
  readShortcutOverrides,
  resolveShellCommands,
  shouldReserveShellShortcut,
  shortcutLabel,
  shortcutLockCodes,
} from '../lib/keyboardShortcuts.js'
import { isStandaloneDisplay } from '../utils/installPlatform.js'

export default function useShellShortcuts(actions) {
  const [overrides, setOverrides] = useState(() => readShortcutOverrides())
  const actionsRef = useRef(actions)
  actionsRef.current = actions

  useEffect(() => {
    const refresh = () => setOverrides(readShortcutOverrides())
    const onStorage = (event) => {
      if (event.key == null || event.key === SHORTCUT_OVERRIDES_STORAGE_KEY) refresh()
    }
    window.addEventListener('storage', onStorage)
    window.addEventListener(SHORTCUT_OVERRIDES_CHANGED_EVENT, refresh)
    return () => {
      window.removeEventListener('storage', onStorage)
      window.removeEventListener(SHORTCUT_OVERRIDES_CHANGED_EVENT, refresh)
    }
  }, [])

  const catalog = useMemo(() => resolveShellCommands(overrides), [overrides])
  const standalone = isStandaloneDisplay()
  const runAction = useCallback((actionId) => {
    const action = actionsRef.current?.[actionId]
    if (!action || action.enabled === false || typeof action.run !== 'function') return false
    return action.run() !== false
  }, [])

  useEffect(() => {
    const onKeyDown = (event) => {
      const command = findShellShortcut(event, catalog)
      if (!command) return
      const handled = runAction(command.id)
      // An unavailable chord is reserved only in an installed display, where
      // allowing Cmd/Ctrl+W through would close the whole app. In an ordinary
      // browser tab, preserve the browser/app default when Möbius did nothing.
      if (!shouldReserveShellShortcut(handled, standalone, command)) return
      event.preventDefault()
      event.stopImmediatePropagation?.()
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  }, [catalog, runAction, standalone])

  // Browser-reserved chords are not uniformly overridable by web content. In an
  // installed display, make one best-effort Keyboard Lock request from the first
  // trusted pointer gesture. Unsupported/denied engines simply retain the
  // command-palette fallback and every chord the document does receive.
  useEffect(() => {
    const keyboard = navigator.keyboard
    const codes = shortcutLockCodes(catalog)
    if (!isStandaloneDisplay() || !keyboard?.lock || !codes.length) return undefined
    let live = true
    let locked = false
    const tryLock = () => {
      window.removeEventListener('pointerdown', tryLock, true)
      Promise.resolve(keyboard.lock(codes)).then(() => {
        if (!live) keyboard.unlock?.()
        else locked = true
      }).catch(() => {})
    }
    window.addEventListener('pointerdown', tryLock, { capture: true, once: true })
    return () => {
      live = false
      window.removeEventListener('pointerdown', tryLock, true)
      if (locked) keyboard.unlock?.()
    }
  }, [catalog])

  const commands = useMemo(() => catalog.map(command => {
    const action = actions?.[command.id]
    return {
      ...command,
      enabled: action?.enabled !== false && typeof action?.run === 'function',
      unavailableReason: action?.unavailableReason || '',
      shortcutLabels: command.bindings.map(binding => shortcutLabel(binding)),
    }
  }), [actions, catalog])

  const disabledFrameActionIds = commands
    .filter(command => command.enabled === false)
    .map(command => command.id)
    .join('\u0000')
  const frameBindings = useMemo(() => {
    const disabled = new Set(disabledFrameActionIds.split('\u0000').filter(Boolean))
    return frameShortcutBindings(catalog.map(command => ({
      ...command,
      enabled: !disabled.has(command.id),
    })), { reserveUnavailable: standalone })
  }, [catalog, disabledFrameActionIds, standalone])
  return { commands, frameBindings, runAction }
}
