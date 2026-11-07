/* Host-attested chat context for passive block sessions (building-apps.md
   "Passive block sessions"). The transcript, never the block's JSON, says
   which chat a block is rendered in, whether it is the newest block of its
   app and intent there, and when the app should re-read its own state.

   `latest` follows document order among the mounted blocks of one chat. A
   block that has seen a newer block of the same app and intent stays
   superseded for the rest of its mount, so a newer block briefly remounting
   (for example when a streamed reply settles) never hands control back to an
   older one. The transcript is not virtualized: the newest messages are
   always mounted, so the newest block is always among the registered ones. */
import { createContext, useContext, useLayoutEffect, useState, useSyncExternalStore } from 'react'

export const REFRESH_REASONS = Object.freeze(['turn', 'visible'])

const follows = (node, other) => Boolean(node && other
  && node.compareDocumentPosition(other) & 4 /* Node.DOCUMENT_POSITION_FOLLOWING */)

export function createAppBlockChat(chatId) {
  const groups = new Map() // key -> Map(id -> { node, superseded })
  const listeners = new Set()
  let refresh = { seq: 0, reason: '' }
  const notify = () => listeners.forEach(listener => listener())
  function recompute(key) {
    const group = groups.get(key)
    if (!group) return
    for (const entry of group.values()) {
      if (!entry.superseded && [...group.values()].some(other => other !== entry && follows(entry.node, other.node))) {
        entry.superseded = true
      }
    }
  }
  return {
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
    refreshSnapshot() { return refresh },
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}

export const AppBlockChatContext = createContext(null)

const NO_REFRESH = Object.freeze({ seq: 0, reason: '' })
const noSubscribe = () => () => {}

/** This block's place in its chat: `{ context, refresh, newestNode, ref }`.
 *  `context` is `{ chatId, latest }`, or null outside a chat transcript (a
 *  preview, a test, a new chat with no id yet). `ref` is a callback ref for
 *  the block's section. */
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
  const context = chat?.chatId ? { chatId: chat.chatId, latest } : null
  return { context, refresh, newestNode: () => chat?.newestNode(key) || null, ref: setNode }
}

export const appBlockChatKey = block => `${block.app}\n${block.intent}`
