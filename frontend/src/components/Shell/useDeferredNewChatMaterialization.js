/* Retry a deferred empty-workspace request when its scene or readiness changes. */
import { useEffect } from 'react'

// The allocator owns network readiness, in-flight work and row reuse. This hook
// observes the shared recovery edge so an offline deferral is not stranded;
// ordinary renders do not retry a server error.
export default function useDeferredNewChatMaterialization({
  pendingNewChatToken,
  materializeNewChatRevision,
  recoveryGeneration,
  modeActive,
  modeTransition,
  viewMode,
  singleScreen,
  workspaceStateRef,
  pendingNewChatRef,
  materializeRef,
}) {
  useEffect(() => {
    if (!pendingNewChatToken) return
    if (modeActive || modeTransition) return
    const pending = pendingNewChatRef.current
    if (!pending || pending.token !== pendingNewChatToken) return
    const ws = workspaceStateRef.current.ws
    const single = ws.viewMode === 'single'
    if (!single || ws.singleScreen != null) {
      // No longer an empty single slot (re-toggled to builder, or a slot was set by
      // another path) — drop the request.
      pendingNewChatRef.current = null
      return
    }
    materializeRef.current?.(pending)
  }, [pendingNewChatToken, materializeNewChatRevision, recoveryGeneration,
      modeActive, modeTransition, viewMode, singleScreen, workspaceStateRef,
      pendingNewChatRef, materializeRef])
}
