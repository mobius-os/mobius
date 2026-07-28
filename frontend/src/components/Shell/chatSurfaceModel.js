// Chat surface ownership keeps Standard's slot and Builder's pane tree as
// physically independent layout worlds while preserving one stable mount per
// chat inside each world.

import { focusedSlotSeed, SINGLE_SLOT_PANE } from './paneModel.js'

export const STANDARD_CHAT_WORLD = 'standard'
export const BUILDER_CHAT_WORLD = 'builder'

export function chatSurfaceKey(world, chatId) {
  return `${world}:chat:${chatId}`
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

  // Legacy workspaces have no singleScreen migration marker yet. The rest of
  // the Standard-world projection treats that absence as the focused Builder
  // item that the first mode transaction will seed, so retain the matching
  // Standard ChatView on the very first render too. An explicit null remains
  // the intentional New Chat landing and must not inherit Builder focus.
  const slot = ('singleScreen' in workspace)
    ? workspace.singleScreen
    : focusedSlotSeed(workspace)
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
export function deriveChatSurfaceLayers(owners, presentedChatByPane) {
  const desiredByWorld = new Map()
  for (const owner of owners) {
    if (!desiredByWorld.has(owner.world)) desiredByWorld.set(owner.world, new Set())
    desiredByWorld.get(owner.world).add(String(owner.chatId))
  }

  const layers = []
  for (const owner of owners) {
    const paneKey = String(owner.paneId)
    const activeId = String(owner.chatId)
    const previousId = presentedChatByPane.get(paneKey)
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
