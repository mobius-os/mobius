import { addBuiltAppForChat, makeTab } from './tabModel.js'

export const WORKSPACE_OPEN_ITEM = 'open-item'
export const PLACE_BESIDE_SOURCE = 'beside-source'
export const ACTIVATE_IN_BACKGROUND = 'background'

// Build completion expresses product intent without naming a tab strip, pane,
// split direction, or breakpoint. Today's flat resolver inserts the target tab
// beside its chat. A pane resolver can interpret the same `beside-source`
// request as "next pane, or create one when wide" without changing producers.
export function builtAppWorkspaceRequest(chatId, appId) {
  const normalizedAppId = Number(appId)
  if (
    chatId == null
    || String(chatId).length === 0
    || !Number.isInteger(normalizedAppId)
    || normalizedAppId <= 0
  ) return null

  return {
    type: WORKSPACE_OPEN_ITEM,
    item: makeTab('app', normalizedAppId),
    source: makeTab('chat', chatId),
    placement: PLACE_BESIDE_SOURCE,
    activation: ACTIVATE_IN_BACKGROUND,
    reason: 'chat-built-app',
  }
}

export function workspaceRequestFromSystemEvent(event) {
  if (event?.type !== 'app_created') return null
  return builtAppWorkspaceRequest(event.chatId, event.appId)
}

export function workspaceRequestsForBuiltApps(arrivals) {
  const requests = []
  for (const arrival of arrivals || []) {
    const request = builtAppWorkspaceRequest(arrival?.chatId, arrival?.appId)
    if (request) requests.push(request)
  }
  return requests
}

function isBuiltAppRequest(request) {
  return request?.type === WORKSPACE_OPEN_ITEM
    && request?.item?.kind === 'app'
    && request?.source?.kind === 'chat'
    && request?.placement === PLACE_BESIDE_SOURCE
    && request?.activation === ACTIVATE_IN_BACKGROUND
}

// Degenerate one-pane resolver. Reverse traversal preserves producer order
// when several apps target the same source chat: A then B stays chat, A, B.
export function applyWorkspaceRequestsToFlatTabs(tabs, requests) {
  let next = tabs
  for (let index = (requests?.length || 0) - 1; index >= 0; index -= 1) {
    const request = requests[index]
    if (!isBuiltAppRequest(request)) continue
    next = addBuiltAppForChat(next, request.source.id, request.item.id)
  }
  return next
}
