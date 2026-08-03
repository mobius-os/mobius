// The New-chat presentation cover is a bridge, not a surface of its own.
//
// An explicit New-chat tap acknowledges the destination before the row exists,
// so the landing paints over the outgoing full-bleed surface for the whole
// allocation. It comes down on exactly one signal: the resolved ChatView
// reporting its first painted frame. That signal belongs to the destination,
// so it never arrives for a destination the owner has already left — Back,
// another chat row, an app, a mode toggle. Without a supersession rule the
// bridge would keep painting New chat over whatever they actually chose.

/**
 * The full-bleed surface key this cover is currently bridging to.
 *
 * While the row is being allocated there is no destination key yet, so the
 * bridge is anchored to the surface the tap left behind: that surface staying
 * put IS the allocation still running. Once the concrete id exists, the
 * destination is the only surface the cover may sit above — returning to the
 * origin at that point is an ordinary navigation away from the new chat.
 */
export function newChatPresentationBridgedKey(presentation) {
  if (!presentation) return null
  return presentation.chatId == null
    ? (presentation.originKey ?? null)
    : `chat:${presentation.chatId}`
}

/**
 * True once some other surface owns the full-bleed box, meaning the cover's
 * destination was superseded and its display-ready signal will never come.
 */
export function newChatPresentationSuperseded(presentation, paintedSurfaceKey) {
  if (!presentation) return false
  const bridged = newChatPresentationBridgedKey(presentation)
  return (paintedSurfaceKey ?? null) !== bridged
}

/** Claim the single in-flight presentation owner synchronously. */
export function claimNewChatPresentation(ownerRef, presentation) {
  if (!presentation || ownerRef.current) return false
  ownerRef.current = presentation
  return true
}

/** Advance an owned allocation while preserving one identity in ref and state. */
export function allocateNewChatPresentation(ownerRef, presentation, chatId) {
  if (ownerRef.current !== presentation) return null
  const allocated = { ...presentation, chatId: String(chatId) }
  ownerRef.current = allocated
  return allocated
}

/** Release only the async operation that still owns the presentation. */
export function releaseNewChatPresentation(ownerRef, presentation) {
  if (ownerRef.current !== presentation) return false
  ownerRef.current = null
  return true
}

/** Release the allocated owner after its concrete chat has painted. */
export function releaseNewChatPresentationForChat(ownerRef, chatId) {
  if (String(ownerRef.current?.chatId ?? '') !== String(chatId)) return false
  ownerRef.current = null
  return true
}
