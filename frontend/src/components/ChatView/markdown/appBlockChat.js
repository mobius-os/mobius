/* Host-attested chat context for passive block sessions (building-apps.md
   "Passive block sessions"). The transcript, never the block's JSON, says
   which chat a block is rendered in, whether it is the newest block of its
   app and intent there, and when the app should re-read its own state.

   `latest` follows document order among the mounted blocks of one chat. A
   block that has seen a newer block of the same app and intent stays
   superseded for the rest of its mount, so a newer block briefly remounting
   (for example when a streamed reply settles) never hands control back to an
   older one. The transcript is not virtualized: the newest messages are
   always mounted, so the newest block is always among the registered ones.

   It also owns this chat's card asks (`ask-v1`): one owner tap sends the
   host-built message through `send` as an ordinary owner message. One ask per
   card is in flight at a time (until the next turn here settles), the same
   message is not sent again within ASK_REPEAT_MS, and the chat takes at most
   ASK_LIMIT asks per ASK_WINDOW_MS. */
import { createContext, useContext, useLayoutEffect, useState, useSyncExternalStore } from 'react'

export const REFRESH_REASONS = Object.freeze(['turn', 'visible'])
export const ASK_REPEAT_MS = 10 * 60 * 1000
export const ASK_WINDOW_MS = 60 * 60 * 1000
export const ASK_LIMIT = 6

const follows = (node, other) => Boolean(node && other
  && node.compareDocumentPosition(other) & 4 /* Node.DOCUMENT_POSITION_FOLLOWING */)

export function createAppBlockChat(chatId, { send = null, now = () => Date.now() } = {}) {
  const groups = new Map() // key -> Map(id -> { node, superseded })
  const listeners = new Set()
  let refresh = { seq: 0, reason: '' }
  // Card asks: `pending` per card key ('sending' until the send settles, then
  // 'asked' until a turn settles), and every accepted ask's message, identity
  // and time. A message whose send failed keeps its identity, so tapping again
  // lets the server recognise a send that did arrive.
  const pending = new Map()
  let sent = []
  const attempts = new Map() // message -> cid of its last unconfirmed send
  let askVersion = 0
  const notify = () => listeners.forEach(listener => listener())
  const changed = () => { askVersion += 1; notify() }
  const recent = () => { sent = sent.filter(entry => now() - entry.at < ASK_WINDOW_MS); return sent }
  function recompute(key) {
    const group = groups.get(key)
    if (!group) return
    for (const entry of group.values()) {
      if (!entry.superseded && [...group.values()].some(other => other !== entry && follows(entry.node, other.node))) {
        entry.superseded = true
      }
    }
  }
  const chat = {
    chatId,
    /** Register (or move) one block's section node under its app/intent key. */
    place(key, id, node) {
      if (!groups.has(key)) groups.set(key, new Map())
      const group = groups.get(key)
      group.set(id, { node, superseded: group.get(id)?.superseded === true })
      recompute(key)
      notify()
    },
    remove(key, id) {
      const group = groups.get(key)
      if (!group?.delete(id)) return
      if (!group.size) groups.delete(key)
      notify()
    },
    /** True until a newer block of the same key has been mounted. */
    latest(key, id) { return groups.get(key)?.get(id)?.superseded !== true },
    /** The newest mounted block's node for a key (for a "jump" link). */
    newestNode(key) {
      const nodes = [...(groups.get(key)?.values() || [])].map(entry => entry.node).filter(Boolean)
      return nodes.find(node => !nodes.some(other => other !== node && follows(node, other))) || null
    },
    /** Ask every mounted session to re-read its state, with a reason. */
    refresh(reason) {
      if (!REFRESH_REASONS.includes(reason)) return
      refresh = { seq: refresh.seq + 1, reason }
      notify()
    },
    /** A turn in this chat settled (visible or not): every card's answered
     *  ask is over, so its control comes back unless askState still limits it. */
    turnSettled() {
      let cleared = false
      for (const [key, state] of pending) if (state === 'asked') { pending.delete(key); cleared = true }
      if (cleared) changed()
    },
    /** What one card's ask control may do now with this exact message:
     *  `ready`, `sending`, `asked` (in flight, or the same message was sent
     *  recently) or `limited` (this chat's hourly limit is reached). */
    askState(key, message) {
      const state = pending.get(key)
      if (state) return state
      const list = recent()
      if (list.some(entry => entry.message === message && now() - entry.at < ASK_REPEAT_MS)) return 'asked'
      return list.length >= ASK_LIMIT ? 'limited' : 'ready'
    },
    /** Send one card's ask as an owner message, if that card is still the
     *  newest of its key and askState allows it. Resolves to whether it was
     *  sent. */
    async ask(key, id, message) {
      if (!send || typeof message !== 'string' || !message || !chat.latest(key, id)
        || chat.askState(key, message) !== 'ready') return false
      pending.set(key, 'sending')
      const cid = attempts.get(message) || (globalThis.crypto?.randomUUID?.() ?? `ask-${now()}-${Math.random().toString(36).slice(2)}`)
      attempts.set(message, cid)
      changed()
      let ok = false
      try { ok = (await send(message, { cid })) === true } catch { ok = false }
      if (ok) {
        attempts.delete(message)
        sent = [...recent(), { message, at: now() }]
        pending.set(key, 'asked')
      } else pending.delete(key)
      changed()
      return ok
    },
    askSnapshot() { return askVersion },
    refreshSnapshot() { return refresh },
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
  return chat
}

export const AppBlockChatContext = createContext(null)

const NO_REFRESH = Object.freeze({ seq: 0, reason: '' })
const noSubscribe = () => () => {}

/** This block's place in its chat: `{ context, refresh, newestNode, ref,
 *  askState, ask }`. `context` is `{ chatId, latest }`, or null outside a chat
 *  transcript (a preview, a test, a new chat with no id yet). `ref` is a
 *  callback ref for the block's section. `askState(message)` and
 *  `ask(message)` are this block's card ask (createAppBlockChat). */
export function useAppBlockChat(key) {
  const chat = useContext(AppBlockChatContext)
  const [id] = useState(() => ({}))
  const [node, setNode] = useState(null)
  // A changed section node (static and live views swap) keeps the block's
  // registration, and with it a superseded verdict; unmounting ends both.
  useLayoutEffect(() => { if (chat && node) chat.place(key, id, node) }, [chat, key, id, node])
  useLayoutEffect(() => () => chat?.remove(key, id), [chat, key, id])
  const latest = useSyncExternalStore(chat ? chat.subscribe : noSubscribe,
    () => chat ? chat.latest(key, id) : true, () => true)
  const refresh = useSyncExternalStore(chat ? chat.subscribe : noSubscribe,
    () => chat ? chat.refreshSnapshot() : NO_REFRESH, () => NO_REFRESH)
  // Re-render when an ask's state changes.
  useSyncExternalStore(chat ? chat.subscribe : noSubscribe, () => chat ? chat.askSnapshot() : 0, () => 0)
  const context = chat?.chatId ? { chatId: chat.chatId, latest } : null
  return { context, refresh, newestNode: () => chat?.newestNode(key) || null, ref: setNode,
    askState: message => chat ? chat.askState(key, message) : 'limited',
    ask: message => chat ? chat.ask(key, id, message) : Promise.resolve(false) }
}

export const appBlockChatKey = block => `${block.app}\n${block.intent}`
