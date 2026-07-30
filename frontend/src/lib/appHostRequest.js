const REQUEST_TYPES = new Set([
  'moebius:new-chat',
  'moebius:open-chat',
  'moebius:open-app',
  'moebius:open-settings',
])

/**
 * Narrow the frame's navigation request wire format before it leaves the
 * exact-window-attributed AppCanvas boundary. Hosts receive one small, stable
 * contract rather than the frame's arbitrary postMessage object.
 */
export function appHostRequest(message) {
  if (!message || !REQUEST_TYPES.has(message.type)) return null
  if (message.type === 'moebius:new-chat') {
    return {
      type: message.type,
      draft: typeof message.draft === 'string' ? message.draft : '',
      autoSend: message.autoSend === true,
    }
  }
  if (message.type === 'moebius:open-chat') {
    if (typeof message.chatId !== 'string' || !message.chatId) return null
    return {
      type: message.type,
      chatId: message.chatId,
      draft: typeof message.draft === 'string' ? message.draft : '',
    }
  }
  if (message.type === 'moebius:open-app') {
    if (!['string', 'number'].includes(typeof message.appId)) return null
    return {
      type: message.type,
      appId: message.appId,
      intent: typeof message.intent === 'string' ? message.intent : '',
    }
  }
  return {
    type: message.type,
    section: typeof message.section === 'string' ? message.section : '',
  }
}
