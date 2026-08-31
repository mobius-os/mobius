// Chat surface ownership keeps Standard's slot and Builder's pane tree as
// physically independent layout worlds while preserving one stable mount per
// chat inside each world.

import { SINGLE_SLOT_PANE } from './paneModel.js'

export const STANDARD_CHAT_WORLD = 'standard'
export const BUILDER_CHAT_WORLD = 'builder'
export const FOCUSED_BUILDER_CHAT_SURFACE = '__builder-focused-chat__'

export function chatSurfaceKey(world, chatId) {
  return `${world}:chat:${chatId}`
}

/**
 * Describe the item currently occupying Standard's full-bleed content
 * surface. Settings and the New Chat landing intentionally return null: they
 * are their own presentation surfaces and never retain an app as a cover.
 */
export function standardContentSurface({ single, fullBleedKey }) {
  if (!single) return null
  const match = /^(app|chat):(.+)$/.exec(String(fullBleedKey || ''))
  if (!match) return null
  return {
    kind: match[1],
    id: match[2],
  }
}

/**
 * Keep an app-backed handoff layer only while the next Standard chat reaches
 * display-ready.
 *
 * Chat-to-chat transitions already have a retained ChatView cover. A direct
 * app-to-chat transition has no outgoing ChatView to hold. Retain the app
 * wrapper for geometry/runtime continuity while CSS replaces its pixels with
 * the same neutral chat-opening surface. Retarget that layer on rapid chat
 * changes so the destination's settlement frame never leaks through.
 */
export function deriveAppToChatCover(previousSurface, currentSurface, cover) {
  if (currentSurface?.kind !== 'chat') return null

  if (cover) {
    return {
      ...cover,
      chatId: currentSurface.id,
    }
  }

  if (previousSurface?.kind !== 'app') return null

  return {
    appId: previousSurface.id,
    chatId: currentSurface.id,
  }
}

function activeChatOwner(workspace, paneId) {
  const pane = workspace.panes[paneId]
  const active = pane?.tabs.find(tab => `chat:${tab.id}` === pane.activeTabKey)
  if (!active || active.kind !== 'chat') return null
  return {
    world: BUILDER_CHAT_WORLD,
    paneId,
    chatId: active.id,
    surfaceKey: chatSurfaceKey(BUILDER_CHAT_WORLD, active.id),
  }
}

/**
 * Return the retained ChatView owners for both workspace worlds.
 *
 * A chat selected in Standard and Builder deliberately appears twice: each
 * mount keeps the geometry, scroll controller, and composer measurements of
 * its own world. Only the painted owner is an active runtime. Builder owners
 * remain keyed by chat id (not pane id), so moving a tab between panes still
 * preserves that world's ChatView identity.
 */
export function deriveChatSurfaceOwners({ workspace, baseProjection, projection }) {
  const owners = []
  const mountedPaneIds = new Set([
    ...(baseProjection?.visibleLeaves || []),
    ...(projection?.visibleLeaves || []),
  ])

  for (const paneId of mountedPaneIds) {
    const owner = activeChatOwner(workspace, paneId)
    if (owner) owners.push(owner)
  }

  // Standard retains a ChatView only for its OWN slot. An absent (legacy/
  // uninitialized) or explicit-null slot is the empty New Chat landing and mounts
  // no Standard ChatView — Standard never borrows Builder's focused chat (two-worlds
  // design).
  const slot = workspace.singleScreen
  if (slot?.kind === 'chat') {
    owners.push({
      world: STANDARD_CHAT_WORLD,
      paneId: SINGLE_SLOT_PANE,
      chatId: slot.id,
      surfaceKey: chatSurfaceKey(STANDARD_CHAT_WORLD, slot.id),
    })
  }

  return owners.sort((a, b) => a.surfaceKey.localeCompare(b.surfaceKey))
}

/**
 * Keep the last painted chat as a same-world cover while a new chat becomes
 * display-ready. A Standard owner never suppresses Builder's cover for the
 * same chat (and vice versa); their layout lifecycles are independent.
 */
export function deriveChatSurfaceLayers(
  owners,
  presentedChatBySurface,
  { focusedBuilderPaneId = null } = {},
) {
  const desiredByWorld = new Map()
  for (const owner of owners) {
    if (!desiredByWorld.has(owner.world)) desiredByWorld.set(owner.world, new Set())
    desiredByWorld.get(owner.world).add(String(owner.chatId))
  }

  // A focused Builder pane is one visual surface even when focus moves between
  // physical panes. Per-pane handoff state cannot cover that move: the outgoing
  // pane and incoming pane each still own their active chat, so neither looks
  // like a chat change.
  const focusedPaneKey = focusedBuilderPaneId == null
    ? null
    : String(focusedBuilderPaneId)
  const focusedOwner = focusedPaneKey == null
    ? null
    : owners.find(owner => (
        owner.world === BUILDER_CHAT_WORLD
        && String(owner.paneId) === focusedPaneKey
      ))
  const presentedFocusedId = presentedChatBySurface.get(
    FOCUSED_BUILDER_CHAT_SURFACE,
  )
  const previousFocusedOwner = focusedOwner
      && presentedFocusedId
      && String(presentedFocusedId) !== String(focusedOwner.chatId)
    ? owners.find(owner => (
        owner.world === BUILDER_CHAT_WORLD
        && String(owner.chatId) === String(presentedFocusedId)
      ))
    : null

  const layers = []
  for (const owner of owners) {
    if (previousFocusedOwner && owner.surfaceKey === previousFocusedOwner.surfaceKey) {
      layers.push({
        ...owner,
        presentationPaneId: focusedPaneKey,
        role: 'held',
      })
      continue
    }
    if (previousFocusedOwner && owner.surfaceKey === focusedOwner.surfaceKey) {
      layers.push({ ...owner, role: 'staging' })
      continue
    }

    const paneKey = String(owner.paneId)
    const activeId = String(owner.chatId)
    const previousId = presentedChatBySurface.get(paneKey)
    const transitioning = previousId && previousId !== activeId
    if (transitioning && !desiredByWorld.get(owner.world)?.has(String(previousId))) {
      layers.push({
        ...owner,
        chatId: previousId,
        surfaceKey: chatSurfaceKey(owner.world, previousId),
        role: 'held',
      })
    }
    layers.push({
      ...owner,
      role: transitioning ? 'staging' : 'active',
    })
  }

  return layers.sort((a, b) => a.surfaceKey.localeCompare(b.surfaceKey))
}
