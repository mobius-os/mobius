/**
 * Mock POST /messages without starting an agent while preserving the real
 * durability contract: a successful send remains on subsequent chat-detail
 * GETs. A bare 202 makes the optimistic row disappear on terminal refresh and
 * turns unrelated rerenders into deterministic false failures.
 */
function messageMatchesAnchor(message, index, key) {
  if (!message || key == null) return false
  const target = String(key)
  if (message.id != null && String(message.id) === target) return true
  if (message.cid != null && String(message.cid) === target) return true
  if (message.ts != null && `${message.role}-${message.ts}` === target) return true
  return `${message.role}-${index}` === target
}

/** Overlay mocked accepted rows while preserving the real anchor-window
 * protocol. The upstream response cannot know about rows intercepted before
 * persistence, so coverage and the returned window must be recomputed after
 * the overlay rather than copied from that stale response. */
export function overlayAcceptedChatDetail(detail, accepted, requestUrl) {
  const persisted = Array.isArray(detail?.messages) ? detail.messages : []
  const persistedCids = new Set(persisted.map(message => message?.cid).filter(Boolean))
  const missing = accepted.filter(message => !persistedCids.has(message.cid))
  const messages = [...persisted, ...missing]
  const baseOffset = Number.isInteger(detail?.offset) ? detail.offset : 0
  const serverTotal = Number.isFinite(Number(detail?.total))
    ? Number(detail.total)
    : baseOffset + persisted.length
  const total = Math.max(serverTotal, baseOffset + persisted.length) + missing.length
  const url = new URL(requestUrl)
  const anchorRequested = url.searchParams.has('anchor')

  if (!anchorRequested) {
    return {
      ...detail,
      messages,
      total,
    }
  }

  const anchorKey = url.searchParams.get('anchor')
  const anchorIndex = messages.findIndex((message, index) => (
    messageMatchesAnchor(message, baseOffset + index, anchorKey)
  ))
  if (anchorIndex >= 0) {
    return {
      ...detail,
      messages: messages.slice(anchorIndex),
      offset: baseOffset + anchorIndex,
      total,
      requested_anchor_found: true,
    }
  }

  const requestedLimit = Number.parseInt(url.searchParams.get('limit') || '', 10)
  const limit = Number.isInteger(requestedLimit) && requestedLimit > 0
    ? requestedLimit
    : 20
  const recent = messages.slice(-limit)
  return {
    ...detail,
    messages: recent,
    offset: Math.max(0, total - recent.length),
    total,
    requested_anchor_found: false,
  }
}

export async function mockAcceptedMessages(page) {
  const acceptedByChat = new Map()

  await page.route(/\/api\/chats\/[0-9a-f-]+(?:\?.*)?$/, async route => {
    const request = route.request()
    if (request.method() !== 'GET') return route.fallback()

    const chatId = new URL(request.url()).pathname.split('/').pop()
    const accepted = acceptedByChat.get(chatId)
    if (!accepted?.length) return route.fallback()

    const response = await route.fetch()
    if (!response.ok()) return route.fulfill({ response })

    const detail = await response.json()
    return route.fulfill({
      response,
      json: overlayAcceptedChatDetail(detail, accepted, request.url()),
    })
  })

  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, async route => {
    const request = route.request()
    if (request.method() !== 'POST') return route.fallback()

    const chatId = new URL(request.url()).pathname.split('/').at(-2)
    const body = request.postDataJSON() || {}
    const message = {
      role: 'user',
      content: body.content || '',
      ts: Date.now(),
      cid: body.cid || `e2e-${crypto.randomUUID()}`,
      ...(body.hidden ? { hidden: true } : {}),
      ...(body.attachments ? { attachments: body.attachments } : {}),
      ...(body.timezone ? { timezone: body.timezone } : {}),
      ...(body.viewport ? { viewport: body.viewport } : {}),
    }
    acceptedByChat.set(chatId, [...(acceptedByChat.get(chatId) || []), message])

    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      json: { status: 'started', message },
    })
  })
}
