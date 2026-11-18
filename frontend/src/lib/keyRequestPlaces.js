/*
 * Where a key request is asked, in the moment: an app's over that app's
 * window while it is open (AppKeyRequestCard.jsx), and an agent's on the card
 * its request_key control saved in the chat that asked (KeyQuestionCard.jsx).
 * Pure and unit-tested. A card only ever answers through the owner-only
 * routes, so nothing here grants anything.
 */

/**
 * The pending requests app ``appId`` made, newest first, for the dialog over
 * that app's window.
 */
export function appKeyRequestsToShow(data, appId) {
  return (data?.requests || []).filter(request => request.status === 'pending'
    && request.requester !== 'agent' && String(request.app) === String(appId))
}

/**
 * The apps (by string id) with a key request waiting for the owner, so the
 * app list can mark them as needing the owner until they open the app.
 */
export function appsWaitingForKeys(data) {
  return new Set((data?.requests || []).filter(request => request.status === 'pending'
    && request.requester !== 'agent' && request.app != null).map(request => String(request.app)))
}

/**
 * The pending requests Settings › Keys answers: every one your agents made,
 * so none is ever stranded, whether its chat still shows its card, its turn
 * was stopped before the owner answered, or its chat was deleted. An app's is
 * answered over its window, which shows it whenever the owner opens the app.
 */
export function settingsKeyRequests(data) {
  return (data?.requests || []).filter(request => request.status === 'pending'
    && request.requester === 'agent')
}
