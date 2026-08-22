import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from 'react'
import * as paneModel from './paneModel.js'
import { enteredEmptySingleScreen } from './newChatPolicy.js'
import { projectFocusedPane } from './workspaceView.js'

/**
 * The live owner of workspace state, synchronous transition previews, persisted
 * presentation state, and content-geometry projection.
 *
 * Callers may attach navigation and New Chat policies through the returned refs
 * without moving those policies into the reducer. Every workspace mutation must
 * use dispatchWorkspace so same-batch transitions compose against workspaceStateRef.
 */
function bootWorkspaceSnapshot(storage, legacyStorage) {
  const legacyRaw = paneModel.readWorkspaceRaw(legacyStorage)
  if (paneModel.isValidWorkspaceBlob(legacyRaw)) {
    return { raw: legacyRaw, source: legacyStorage }
  }
  return { raw: paneModel.readWorkspaceRaw(storage), source: storage }
}

export default function useWorkspaceSession({ storage, legacyStorage = null }) {
  const [bootSnapshot] = useState(
    () => bootWorkspaceSnapshot(storage, legacyStorage),
  )
  const [workspaceState, dispatchWorkspaceRaw] = useReducer(
    paneModel.workspaceReducer,
    undefined,
    () => paneModel.initialWorkspaceState(
      paneModel.parseWorkspace(bootSnapshot.raw),
    ),
  )
  const workspace = workspaceState.ws
  const blobValid = paneModel.isValidWorkspaceBlob(bootSnapshot.raw)
  const replaceImplicitBootTab = !blobValid
    && Object.keys(workspace.panes).length === 1
    && paneModel.flatten(workspace).length <= 1

  const workspaceStateRef = useRef(workspaceState)
  workspaceStateRef.current = workspaceState
  const closedTabsRef = useRef([])
  const dragActiveRef = useRef(false)
  const onWorkspaceTransitionRef = useRef(null)
  const requestEmptySingleNewChatRef = useRef(null)

  const [focusedPaneViewId, setFocusedPaneViewIdState] = useState(
    () => paneModel.resolveInitialFocusedPaneView(
      workspace, paneModel.readFocusedPaneView(bootSnapshot.source),
    ),
  )
  const focusedPaneViewIdRef = useRef(focusedPaneViewId)
  const setFocusedPaneViewId = useCallback((paneId) => {
    focusedPaneViewIdRef.current = paneId
    setFocusedPaneViewIdState(paneId)
  }, [])

  const dispatchWorkspace = useCallback((action) => {
    const prev = workspaceStateRef.current
    let resolvedAction = action
    let restoreRecord = null
    if (action?.type === 'RESTORE_CLOSED_TAB') {
      while (closedTabsRef.current.length) {
        const candidate = closedTabsRef.current[closedTabsRef.current.length - 1]
        const candidateAction = { type: 'RESTORE_CLOSED_TAB_RECORD', record: candidate }
        const candidateState = paneModel.workspaceReducer(prev, candidateAction)
        closedTabsRef.current.pop()
        if (candidateState !== prev) {
          resolvedAction = candidateAction
          restoreRecord = candidate
          break
        }
      }
      if (!restoreRecord) return false
    }
    const closedRecord = resolvedAction?.type === 'CLOSE_TAB'
      ? paneModel.closedTabRecord(prev.ws, resolvedAction.tabKey)
      : null
    const next = paneModel.workspaceReducer(prev, resolvedAction)
    if (resolvedAction?.type === 'CLOSE_TAB' && resolvedAction.reason === 'deleted') {
      closedTabsRef.current = closedTabsRef.current.filter(
        record => record.tabKey !== resolvedAction.tabKey,
      )
    } else if (closedRecord && next.ws !== prev.ws) {
      closedTabsRef.current.push({
        ...closedRecord,
        restoreViewMode: prev.ws.viewMode !== next.ws.viewMode,
      })
      if (closedTabsRef.current.length > 20) closedTabsRef.current.shift()
    }
    workspaceStateRef.current = next
    const enteredEmptySingle = next.ws !== prev.ws
      && enteredEmptySingleScreen(prev.ws, next.ws)
    if (next.ws !== prev.ws) {
      onWorkspaceTransitionRef.current?.(prev.ws, next.ws)
      const expanded = focusedPaneViewIdRef.current
      if (expanded != null) {
        const paneIds = Object.keys(next.ws.panes)
        if (paneIds.length <= 1) {
          setFocusedPaneViewId(null)
        } else if (next.ws.focusedPaneId !== prev.ws.focusedPaneId
            || !next.ws.panes[expanded]) {
          setFocusedPaneViewId(next.ws.focusedPaneId)
        }
      }
    }
    dispatchWorkspaceRaw(resolvedAction)
    if (enteredEmptySingle) requestEmptySingleNewChatRef.current?.()
    return next !== prev
  }, [setFocusedPaneViewId])

  const contentElRef = useRef(null)
  const [contentRect, setContentRect] = useState({ w: 0, h: 0 })
  const pendingContentRectRef = useRef(null)
  const contentRectRef = useRef(contentRect)
  contentRectRef.current = contentRect

  const syncContentRect = useCallback(({ settlePending = false } = {}) => {
    const el = contentElRef.current
    if (!el) return
    const w = Math.round(el.clientWidth)
    const h = Math.round(el.clientHeight)
    if (pendingContentRectRef.current && !settlePending) return
    if (settlePending) pendingContentRectRef.current = null
    setContentRect(prev => {
      if (prev.w === w && prev.h === h) return prev
      return { w, h }
    })
  }, [])

  const primeContentRect = useCallback((nextRect) => {
    const w = Math.round(nextRect?.w || 0)
    const h = Math.round(nextRect?.h || 0)
    pendingContentRectRef.current = { w, h }
    setContentRect(prev => (prev.w === w && prev.h === h ? prev : { w, h }))
  }, [])

  useEffect(() => {
    const el = contentElRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => syncContentRect())
    observer.observe(el)
    return () => { observer.disconnect() }
  }, [syncContentRect])

  const workspaceMode = useMemo(
    () => paneModel.modeForRect(contentRect),
    [contentRect],
  )
  const baseProjection = useMemo(
    () => paneModel.projectLayout(workspace, workspaceMode, contentRect),
    [workspace, workspaceMode, contentRect],
  )
  const projection = useMemo(
    () => projectFocusedPane(
      baseProjection, workspace, focusedPaneViewId, contentRect,
    ),
    [baseProjection, workspace, focusedPaneViewId, contentRect],
  )
  const visiblePaneIds = useMemo(
    () => new Set(projection.visibleLeaves),
    [projection],
  )

  useEffect(() => {
    paneModel.writeFocusedPaneView(focusedPaneViewId, storage)
  }, [focusedPaneViewId, storage])

  // The workspace is one durable browser-owned snapshot. `legacyStorage` is
  // only the legacy session value: prefer it once during this live tab's
  // upgrade, copy the normalized result durably, then remove it so it can never
  // override a newer cross-launch snapshot on a later reload.
  useEffect(() => {
    try {
      storage.setItem(
        paneModel.STORAGE_KEY,
        paneModel.serializeWorkspace(workspace),
      )
    } catch {
      // Private mode/quota may reject writes; keep the session copy as the only
      // available recovery source rather than deleting it after a failed copy.
      return
    }
    if (!legacyStorage || legacyStorage === storage) return
    try {
      legacyStorage.removeItem(paneModel.STORAGE_KEY)
      legacyStorage.removeItem(paneModel.FOCUSED_PANE_VIEW_KEY)
    } catch { /* the durable copy already succeeded */ }
  }, [legacyStorage, storage, workspace])

  const toggleFocusedPaneView = useCallback((paneId) => {
    const ws = workspaceStateRef.current.ws
    if (!ws.panes[paneId] || Object.keys(ws.panes).length <= 1) {
      setFocusedPaneViewId(null)
      return
    }
    if (focusedPaneViewIdRef.current === paneId) {
      setFocusedPaneViewId(null)
      return
    }
    dispatchWorkspace({ type: 'FOCUS', paneId })
    setFocusedPaneViewId(paneId)
  }, [dispatchWorkspace, setFocusedPaneViewId])

  const persistWorkspaceSnapshot = useCallback(() => {
    try {
      storage.setItem(
        paneModel.STORAGE_KEY,
        paneModel.serializeWorkspace(workspaceStateRef.current.ws),
      )
      paneModel.writeFocusedPaneView(focusedPaneViewIdRef.current, storage)
    } catch {
      // Private mode/quota may reject writes; the mounted in-memory owner remains
      // authoritative for this session.
    }
  }, [storage])

  return {
    workspace,
    workspaceStateRef,
    dispatchWorkspace,
    blobValid,
    replaceImplicitBootTab,
    dragActiveRef,
    onWorkspaceTransitionRef,
    requestEmptySingleNewChatRef,
    focusedPaneViewId,
    focusedPaneViewIdRef,
    setFocusedPaneViewId,
    toggleFocusedPaneView,
    contentElRef,
    contentRect,
    contentRectRef,
    primeContentRect,
    syncContentRect,
    workspaceMode,
    baseProjection,
    projection,
    visiblePaneIds,
    persistWorkspaceSnapshot,
  }
}
