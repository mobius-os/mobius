/**
 * Mock POST /messages without starting an agent while preserving the real
 * durability contract: a successful send remains on subsequent chat-detail
 * GETs. A bare 202 makes the optimistic row disappear on terminal refresh and
 * turns unrelated rerenders into deterministic false failures.
 */
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
    const persisted = Array.isArray(detail.messages) ? detail.messages : []
    const persistedCids = new Set(persisted.map(message => message?.cid).filter(Boolean))
    const missing = accepted.filter(message => !persistedCids.has(message.cid))
    const messages = [...persisted, ...missing]

    return route.fulfill({
      response,
      json: {
        ...detail,
        messages,
        total: Math.max(Number(detail.total) || 0, messages.length),
      },
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
