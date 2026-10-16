// The real App over an in-page fake backend. Records every /api request so a
// test can pin which reads opening a chat costs. `?skew=<ms>` runs the clock
// ahead, so everything the previous visit persisted is past its staleTime.
const json = (value, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { 'Content-Type': 'application/json' },
})
const skewMs = Number(new URLSearchParams(location.search).get('skew') || 0)
if (skewMs) {
  const nativeNow = Date.now.bind(Date)
  Date.now = () => nativeNow() + skewMs
}

const now = Date.now()
const iso = offset => new Date(now - offset).toISOString()
const chatRow = (id, offset) => ({
  id, title: `Chat ${id}`, created_at: iso(offset + 1000), updated_at: iso(offset),
  running: false, has_messages: true, provider: 'claude', model: null,
})
const chats = [chatRow('target', 1000), chatRow('previous', 2000), chatRow('other', 3000)]
const message = (chatId, i) => i % 2
  ? { role: 'assistant', id: `${chatId}-a${i}`, ts: now / 1000 - 100 + i, blocks: [{ type: 'text', content: `Reply ${i}` }] }
  : { role: 'user', id: `${chatId}-u${i}`, ts: now / 1000 - 100 + i, content: `Question ${i}` }
const detail = chatId => {
  const messages = Array.from({ length: 6 }, (_, i) => message(chatId, i))
  return {
    ...chats.find(chat => chat.id === chatId), messages, offset: 0, total: messages.length,
    run_id: null, run_status: null, runtime_revision: 3, active_assistant_message_id: null,
    goal: null, handoff: null, pending_messages: [], pending_question_id: null,
    waits: [], background_helpers: [],
  }
}
const runtimeOf = chatId => {
  const { messages: _messages, offset: _offset, total: _total, ...runtime } = detail(chatId)
  return runtime
}

const requests = []
const nativeFetch = window.fetch.bind(window)
let systemOpened = false
const encode = event => new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`)

window.fetch = async (input, options = {}) => {
  const parsed = new URL(String(input?.url || input), location.href)
  if (!parsed.pathname.startsWith('/api/')) return nativeFetch(input, options)
  const method = (options.method || input?.method || 'GET').toUpperCase()
  const path = parsed.pathname
  requests.push(`${method === 'GET' ? '' : `${method} `}${path}${parsed.search}`)
  // Every response takes a little time, as it does over a real network.
  await new Promise(resolve => setTimeout(resolve, 20))
  if (path === '/api/events/system') {
    return new Response(new ReadableStream({ start(controller) {
      systemOpened = true
      controller.enqueue(encode({ type: 'system_stream_open', subscriptionId: 'sub-1' }))
    } }), { headers: { 'Content-Type': 'text/event-stream' } })
  }
  if (path === '/api/chats') return json(chats)
  let match = path.match(/^\/api\/chats\/([^/]+)$/)
  if (match) return chats.some(chat => chat.id === match[1]) ? json(detail(match[1])) : json({ detail: 'Not found' }, 404)
  match = path.match(/^\/api\/chats\/([^/]+)\/runtime$/)
  if (match) return json(runtimeOf(match[1]))
  if (/^\/api\/chats\/[^/]+\/stream$/.test(path)) return new Response('', { status: 204 })
  if (/\/activity$/.test(path)) return json({ events: [], next_before: null })
  if (/\/compact-progress$/.test(path)) return json({ progress: null })
  if (path.startsWith('/api/apps/chat-artifacts/')) return json([])
  if (['/api/apps/', '/api/projects', '/api/projects/templates', '/api/shared-apps'].includes(path)) return json([])
  if (path === '/api/notifications/unread-count' || path === '/api/notifications/new-count') return json({ count: 0 })
  if (path === '/api/version') return json({ version: 'test', serving_source: 'platform', frontend_source: 'platform' })
  if (path === '/api/ready') return json({ ready: true, boot_id: 'boot-1' })
  if (path === '/api/owner/walkthrough') return json({ completed: true })
  if (path === '/api/auth/providers/status') return json({ providers: [] })
  if (path === '/api/models') return json({ providers: [], models: [] })
  return json({})
}

localStorage.setItem('token', 'fixture-token')

window.chatOpen = {
  get systemOpened() { return systemOpened },
  peek() { return requests.slice() },
  take() { return requests.splice(0) },
}

const [{ default: App }, { createRoot }] = await Promise.all([
  import('../../../App.jsx'),
  import('react-dom/client'),
])
createRoot(document.getElementById('root')).render(<App />)
