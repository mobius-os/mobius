// window.mobius.live — shared live rooms for one app's frames.
//
//   const room = window.mobius.live.join('match-AB12', { name: 'Ana', max: 4 })
//   room.onStatus((s) => ...)        // 'connecting' | 'open' | 'error' | 'closed'
//   room.onPresence((members) => ...) // [{id, name}] — everyone in the room, incl. you
//   room.onMessage(({ from, data }) => ...)
//   room.send(data)                  // to everyone else
//   room.send(data, [memberId, ...]) // to specific members
//   room.sync()                      // ask the relay for a fresh member list
//   room.close()
//   room.status, room.you ({id, name} once open), room.members
//
// A room is a relay: it carries small JSON messages between the frames that
// joined the same name in the same app. Nothing is stored. Messages may be
// dropped for a slow receiver, so send state that a newer message supersedes.
// The relay is `/api/apps/{appId}/live/{room}` (backend/app/routes/live_rooms.py);
// a signed-in session and a guest on the app's public page can both join.

const RETRY_MS = [400, 1000, 2000, 4000]
const FATAL = new Set([4401, 4403, 4413, 4429])

function socketUrl(appId, name, token, opts) {
  const base = new URL(`/api/apps/${encodeURIComponent(appId)}/live/${encodeURIComponent(name)}`, globalThis.location.href)
  base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:'
  base.searchParams.set('token', token)
  if (opts.name) base.searchParams.set('name', String(opts.name).slice(0, 24))
  if (opts.max) base.searchParams.set('max', String(opts.max))
  return base.toString()
}

export function makeLive({ appId, getToken }) {
  const open = new Set()

  function join(name, opts = {}) {
    if (typeof name !== 'string' || !name) throw new Error('mobius.live.join: a room name is required')
    const handlers = { message: new Set(), presence: new Set(), status: new Set() }
    const room = { status: 'connecting', you: null, members: [], error: null }
    let ws = null, closed = false, attempt = 0, timer = null

    const emit = (set, value) => { for (const cb of [...set]) { try { cb(value) } catch (e) {} } }
    const setStatus = (status, error = null) => {
      if (room.status === status && room.error === error) return
      room.status = status; room.error = error
      emit(handlers.status, status)
    }
    const sub = (set) => (cb) => {
      if (typeof cb !== 'function') return () => {}
      set.add(cb)
      return () => { set.delete(cb) }
    }

    async function connect() {
      if (closed) return
      setStatus('connecting')
      let token = null
      try { token = await getToken() } catch (e) {}
      if (closed) return
      if (!token) { setStatus('error', 'unauthorized'); return }
      try { ws = new WebSocket(socketUrl(appId, name, token, opts)) } catch (e) { setStatus('error', 'unavailable'); return }
      const mine = ws
      mine.onmessage = (event) => {
        let msg
        try { msg = JSON.parse(event.data) } catch (e) { return }
        if (!msg || typeof msg !== 'object') return
        if (msg.t === 'welcome') {
          attempt = 0
          room.you = msg.you; room.members = msg.members || []
          setStatus('open')
          emit(handlers.presence, room.members)
        } else if (msg.t === 'presence') {
          room.members = msg.members || []
          emit(handlers.presence, room.members)
        } else if (msg.t === 'msg') {
          emit(handlers.message, { from: msg.from, data: msg.data })
        }
      }
      mine.onclose = (event) => {
        if (ws === mine) ws = null
        if (closed) return
        if (FATAL.has(event.code)) {
          setStatus('error', event.code === 4403 ? 'full' : event.code === 4401 ? 'unauthorized' : 'rejected')
          return
        }
        setStatus('connecting')
        timer = setTimeout(connect, RETRY_MS[Math.min(attempt++, RETRY_MS.length - 1)])
      }
      mine.onerror = () => {}
    }

    const transmit = (payload) => {
      if (ws && ws.readyState === 1) { ws.send(JSON.stringify(payload)); return true }
      return false
    }

    Object.assign(room, {
      onMessage: sub(handlers.message),
      onPresence: sub(handlers.presence),
      onStatus: sub(handlers.status),
      send(data, to) {
        const frame = { t: 'send', data }
        if (Array.isArray(to) && to.length) frame.to = to
        return transmit(frame)
      },
      sync() { return transmit({ t: 'sync' }) },
      close() {
        if (closed) return
        closed = true
        clearTimeout(timer)
        open.delete(room)
        try { ws?.close(1000) } catch (e) {}
        ws = null
        room.you = null; room.members = []
        setStatus('closed')
      },
    })
    open.add(room)
    connect()
    return room
  }

  return {
    join,
    _destroy() { for (const room of [...open]) room.close() },
  }
}
