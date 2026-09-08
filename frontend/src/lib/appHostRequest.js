const REQUEST_TYPES = new Set([
  'moebius:new-chat',
  'moebius:open-chat',
  'moebius:open-app',
  'moebius:open-settings',
  'moebius:projects',
  'moebius:chat-control',
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
  if (message.type === 'moebius:projects') {
    const actions = new Set(['list', 'migrate', 'create', 'open', 'browse'])
    if (
      typeof message.requestId !== 'string'
      || !/^projects:[a-z0-9]+:[a-z0-9]+$/i.test(message.requestId)
      || !actions.has(message.action)
    ) return null
    return {
      type: message.type,
      requestId: message.requestId,
      action: message.action,
      projectId: typeof message.projectId === 'string' ? message.projectId.slice(0, 128) : '',
      templateId: typeof message.templateId === 'string' ? message.templateId.slice(0, 128) : '',
      name: typeof message.name === 'string' ? message.name.trim().slice(0, 256) : '',
    }
  }
  if (message.type === 'moebius:chat-control') {
    const actions = new Set(['status', 'stop'])
    if (
      typeof message.requestId !== 'string'
      || !/^chat-control:[a-z0-9]+:[a-z0-9]+$/i.test(message.requestId)
      || !actions.has(message.action)
      || typeof message.chatId !== 'string'
      || !message.chatId.trim()
    ) return null
    return {
      type: message.type,
      requestId: message.requestId,
      action: message.action,
      chatId: message.chatId.trim().slice(0, 128),
    }
  }
  return {
    type: message.type,
    section: typeof message.section === 'string' ? message.section : '',
  }
}
