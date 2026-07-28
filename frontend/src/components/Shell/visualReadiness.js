/* Workspace visual readiness keeps automation independent of private DOM classes. */

export const WORKSPACE_VISUAL_SETTLED = 'settled'
export const WORKSPACE_VISUAL_TRANSITIONING = 'transitioning'

/**
 * A capture is stable once the workspace mode is idle and the chat world that
 * is actually painted has one active owner per surface. Retained hidden worlds
 * may remain mid-handoff indefinitely; they are mounted for continuity, not
 * visual ownership, and therefore never block capture readiness.
 */
export function deriveWorkspaceVisualState({
  modeTransition,
  chatPanesVisible,
  chatPaneLayers,
  paintedChatWorld,
}) {
  if (modeTransition) return WORKSPACE_VISUAL_TRANSITIONING
  if (!chatPanesVisible) return WORKSPACE_VISUAL_SETTLED
  const paintedHandoff = chatPaneLayers.some(layer => (
    layer.world === paintedChatWorld && layer.role !== 'active'
  ))
  return paintedHandoff ? WORKSPACE_VISUAL_TRANSITIONING : WORKSPACE_VISUAL_SETTLED
}
