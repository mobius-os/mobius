import { useCallback, useEffect, useMemo, useRef } from 'react'
import {
  findShellShortcut,
  frameShortcutBindings,
  resolveShellCommands,
  shortcutLabel,
  shortcutLockCodes,
} from '../lib/keyboardShortcuts.js'
import { isStandaloneDisplay } from '../utils/installPlatform.js'

export default function useShellShortcuts(actions) {
  const actionsRef = useRef(actions)
  actionsRef.current = actions

  const catalog = useMemo(() => resolveShellCommands(), [])
  const runAction = useCallback((actionId) => {
    const action = actionsRef.current?.[actionId]
    if (!action || action.enabled === false || typeof action.run !== 'function') return false
    return action.run() !== false
  }, [])

  useEffect(() => {
    const onKeyDown = (event) => {
      const command = findShellShortcut(event, catalog)
      if (!command) return
      // A reserved chord belongs to the shell even when its action is currently
      // unavailable. Preventing the native default keeps Cmd+W from closing the
      // installed app just because Standard mode has no closable Builder tab.
      event.preventDefault()
      event.stopImmediatePropagation?.()
      runAction(command.id)
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  }, [catalog, runAction])

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

  const frameBindings = useMemo(() => frameShortcutBindings(catalog), [catalog])
  return { commands, frameBindings, runAction }
}
