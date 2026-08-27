import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BASE, probeDeletion } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import { appVersionKey } from '../../lib/appVersion.js'
import {
  APP_LRU_STORAGE_KEY,
  mergeAppLru,
  parseStoredAppLru,
  requestAppCodeWarm,
  selectAppsToWarm,
} from '../../lib/appPrecache.js'
import { coldRestoredCanvasAppId } from '../../hooks/useNavigation.js'
import * as tabModel from './tabModel.js'
import {
  appFrameCacheMaxForDeviceMemory,
  deriveRenderedAppIds,
} from './appFrameCache.js'

/** Own mounted app-frame identity, bounded recency, warming, and eviction. */
export default function useAppFrameCache({
  apps,
  appsQuery,
  visibleAppIds,
  workspace,
  openTabs,
  queryClient,
  navStackRef,
  workspaceStateRef,
  retireAppHistory,
  tombstoneRoute,
  dispatchWorkspace,
}) {
  const [appCacheMax] = useState(() => appFrameCacheMaxForDeviceMemory(
    typeof navigator === 'undefined' ? undefined : navigator.deviceMemory,
  ))
  const warmLruRef = useRef(
    coldRestoredCanvasAppId != null ? [String(coldRestoredCanvasAppId)] : [],
  )
  const [warmVersion, setWarmVersion] = useState(0)
  const seenAppIdsRef = useRef(new Set())
  const initialSlotReconciledRef = useRef(false)
  const coldRestoreCheckedRef = useRef(false)
  const warmedOnLoadRef = useRef(false)

  const versionForApp = useCallback((id) => {
    const app = apps.find(row => String(row.id) === String(id))
    return appVersionKey(app?.updated_at)
  }, [apps])

  const dropFromWarmLru = useCallback((matches) => {
    if (!warmLruRef.current.some(matches)) return
    warmLruRef.current = warmLruRef.current.filter(id => !matches(id))
    setWarmVersion(version => version + 1)
  }, [])

  const renderedAppIds = useMemo(() => {
    // warmVersion is the render signal for the ref-backed LRU. Reading it here
    // makes that relationship explicit to both maintainers and hook analysis.
    void warmVersion
    return deriveRenderedAppIds({
      visibleAppIds,
      singleScreen: workspace.singleScreen,
      warmIds: warmLruRef.current,
      max: appCacheMax,
    })
  }, [appCacheMax, visibleAppIds, warmVersion, workspace.singleScreen])

  // Visible apps must enter the mounted set synchronously; this effect only
  // rotates the bounded hidden remainder for later renders.
  useEffect(() => {
    const visible = [...visibleAppIds].map(String)
    const previous = warmLruRef.current
    const merged = [
      ...visible,
      ...previous.filter(id => !visible.includes(id)),
    ].slice(0, appCacheMax)
    const changed = merged.length !== previous.length
      || merged.some((id, index) => id !== previous[index])
    if (changed) {
      warmLruRef.current = merged
      setWarmVersion(version => version + 1)
    }
  }, [appCacheMax, visibleAppIds])

  const appsLiveFetched = appsQuery.isSuccess && appsQuery.isFetchedAfterMount

  const [initialAppLru] = useState(() => {
    try {
      return parseStoredAppLru(localStorage.getItem(APP_LRU_STORAGE_KEY))
    } catch {
      return []
    }
  })
  useEffect(() => {
    if (renderedAppIds.length === 0) return
    try {
      const stored = parseStoredAppLru(localStorage.getItem(APP_LRU_STORAGE_KEY))
      localStorage.setItem(
        APP_LRU_STORAGE_KEY,
        JSON.stringify(mergeAppLru(renderedAppIds, stored)),
      )
    } catch {
      // Storage unavailable: mounted identity remains correct in memory.
    }
  }, [renderedAppIds])

  const warmAppCode = useCallback(async (app) => {
    try {
      const token = await queryClient.fetchQuery({
        queryKey: appQueries.token.key(app.id),
        queryFn: () => appQueries.token.fetch(app.id),
        staleTime: 5 * 60_000,
      })
      const version = appVersionKey(app.updated_at)
      const frameRev = (
        typeof document !== 'undefined'
        && document.querySelector('meta[name="mobius-frame-rev"]')?.content
      ) || ''
      const frameUrl = `${BASE}/api/apps/${app.id}/frame?v=${encodeURIComponent(version)}${frameRev ? `-${frameRev}` : ''}`
      const moduleUrl = `${BASE}/api/apps/${app.id}/module?v=${encodeURIComponent(version)}&token=${encodeURIComponent(token)}`
      await requestAppCodeWarm({ frameUrl, moduleUrl })
    } catch {
      // Warming is speculative and must never interfere with navigation.
    }
  }, [queryClient])

  const closeRemovedApp = useCallback((appId, reason) => {
    const id = String(appId)
    retireAppHistory(appId, reason)
    tombstoneRoute('app', appId)
    dispatchWorkspace({
      type: 'CLOSE_TAB',
      tabKey: tabModel.tabKey(tabModel.makeTab('app', id)),
      reason: 'deleted',
    })
    dropFromWarmLru(candidate => String(candidate) === id)
  }, [dispatchWorkspace, dropFromWarmLru, retireAppHistory, tombstoneRoute])

  // Only a live-list present→absent transition is deletion evidence for apps
  // seen in this session. Initial misses can be stale offline results.
  useEffect(() => {
    if (!appsLiveFetched) return
    const liveIds = new Set(apps.map(app => app.id))
    for (const id of liveIds) seenAppIdsRef.current.add(id)
    const candidates = new Set(renderedAppIds.map(String))
    for (const tab of openTabs) {
      if (tab.kind === 'app') candidates.add(String(tab.id))
    }
    if (candidates.size === 0) return
    const navHeld = new Set(
      navStackRef.current
        .filter(entry => entry.view === 'canvas' && entry.appId != null)
        .map(entry => String(entry.appId)),
    )
    for (const id of visibleAppIds) navHeld.delete(String(id))
    const stale = [...candidates].filter(id => {
      const numericId = Number(id)
      return !navHeld.has(id)
        && !liveIds.has(numericId)
        && seenAppIdsRef.current.has(numericId)
    })
    if (stale.length === 0) return
    const staleSet = new Set(stale)
    navStackRef.current = navStackRef.current.filter(
      entry => !(entry.view === 'canvas' && staleSet.has(String(entry.appId))),
    )
    for (const id of stale) closeRemovedApp(id, 'uninstalled')
  }, [
    apps,
    appsLiveFetched,
    closeRemovedApp,
    navStackRef,
    openTabs,
    renderedAppIds,
    visibleAppIds,
  ])

  // A Standard-world slot restored from disk has no seen-present history.
  // Confirm the list hint through the authoritative per-app deletion probe.
  useEffect(() => {
    if (!appsLiveFetched || initialSlotReconciledRef.current) return
    initialSlotReconciledRef.current = true
    const slot = workspaceStateRef.current.ws.singleScreen
    if (!slot || slot.kind !== 'app') return
    if (apps.some(app => Number(app.id) === Number(slot.id))) return
    const slotId = slot.id
    let cancelled = false
    void (async () => {
      const verdict = await probeDeletion(`/apps/${encodeURIComponent(slotId)}`)
      const current = workspaceStateRef.current.ws.singleScreen
      if (
        cancelled
        || !current
        || current.kind !== 'app'
        || Number(current.id) !== Number(slotId)
      ) return
      if (verdict === 'deleted') closeRemovedApp(slotId, 'uninstalled')
    })()
    return () => { cancelled = true }
  }, [apps, appsLiveFetched, closeRemovedApp, workspaceStateRef])

  // Reconcile the optimistic pre-workspace cold-restore slot once.
  useEffect(() => {
    if (!appsLiveFetched || coldRestoreCheckedRef.current) return
    coldRestoreCheckedRef.current = true
    if (coldRestoredCanvasAppId == null) return
    const live = new Set(apps.map(app => app.id))
    if (live.has(coldRestoredCanvasAppId)) return
    closeRemovedApp(coldRestoredCanvasAppId, 'cold-restore-gone')
  }, [apps, appsLiveFetched, closeRemovedApp])

  useEffect(() => {
    if (warmedOnLoadRef.current || !appsLiveFetched || apps.length === 0) return
    warmedOnLoadRef.current = true
    if (navigator.connection?.saveData) return
    const toWarm = selectAppsToWarm(apps, initialAppLru)
    if (toWarm.length === 0) return
    const idle = typeof requestIdleCallback === 'function'
      ? callback => requestIdleCallback(callback, { timeout: 5000 })
      : callback => setTimeout(callback, 1500)
    idle(() => { for (const app of toWarm) void warmAppCode(app) })
  }, [apps, appsLiveFetched, initialAppLru, warmAppCode])

  return {
    appsLiveFetched,
    dropFromWarmLru,
    renderedAppIds,
    versionForApp,
    warmAppCode,
  }
}
