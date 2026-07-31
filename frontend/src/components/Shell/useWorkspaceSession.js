import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from 'react'
import * as tabModel from './tabModel.js'
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
export default function useWorkspaceSession({ storage }) {
  const [legacyOpenTabs] = useState(() => tabModel.readOpenTabs(storage))
  const [workspaceState, dispatchWorkspaceRaw] = useReducer(
    paneModel.workspaceReducer,
    undefined,
    () => paneModel.initialWorkspaceState(paneModel.parseWorkspace(
      paneModel.readWorkspaceRaw(storage),
      { fallbackTabs: legacyOpenTabs },
    )),
  )
  const workspace = workspaceState.ws
  const [blobValid] = useState(
    () => paneModel.isValidWorkspaceBlob(paneModel.readWorkspaceRaw(storage)),
  )
  const replaceImplicitBootTab = !blobValid
    && legacyOpenTabs.length === 0
    && Object.keys(workspace.panes).length === 1
    && paneModel.flatten(workspace).length <= 1

  const workspaceStateRef = useRef(workspaceState)
  workspaceStateRef.current = workspaceState
  const dragActiveRef = useRef(false)
  const onWorkspaceTransitionRef = useRef(null)
  const requestEmptySingleNewChatRef = useRef(null)

  const [focusedPaneViewId, setFocusedPaneViewIdState] = useState(
    () => paneModel.resolveInitialFocusedPaneView(
      workspace, paneModel.readFocusedPaneView(storage),
    ),
  )
  const focusedPaneViewIdRef = useRef(focusedPaneViewId)
  const setFocusedPaneViewId = useCallback((paneId) => {
    focusedPaneViewIdRef.current = paneId
    setFocusedPaneViewIdState(paneId)
  }, [])

  const dispatchWorkspace = useCallback((action) => {
    const prev = workspaceStateRef.current
    const next = paneModel.workspaceReducer(prev, action)
    workspaceStateRef.current = next
    const enteredEmptySingle = next.ws !== prev.ws
      && enteredEmptySingleScreen(
        prev.ws, next.ws, paneModel.WORKSPACE_SPLITS_ENABLED,
      )
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
    dispatchWorkspaceRaw(action)
    if (enteredEmptySingle) requestEmptySingleNewChatRef.current?.()
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
      if (!paneModel.WORKSPACE_SPLITS_ENABLED
          && Object.keys(workspaceStateRef.current.ws.panes).length <= 1) return prev
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
    legacyOpenTabs,
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
