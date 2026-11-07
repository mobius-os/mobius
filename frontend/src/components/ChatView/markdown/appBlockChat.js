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

   It also sends this chat's card asks (`ask-v1`): one owner tap sends the
   host-built message through `send` as an ordinary owner message, with a
   client message id derived from the chat, the block, its references and
   the hour (askCid). The server already treats a repeated id as the same
   message, so a retry, a reload, a second tab or a concurrent tap of the same
   ask posts it at most once that hour. Nothing here counts or remembers sends
   for safety; `pending` only shows the tapped card as waiting. */
import { createContext, useContext, useLayoutEffect, useState, useSyncExternalStore } from 'react'

export const REFRESH_REASONS = Object.freeze(['turn', 'visible'])
export const ASK_PERIOD_MS = 60 * 60 * 1000

/** What makes one block itself, for its asks: its app, intent, title and the
 *  intents it lists and acts on, as the transcript printed them. */
export const blockFingerprint = block => JSON.stringify([block.app, block.intent, block.title,
  (block.items || []).map(item => item.intent), [block.action, ...(block.items || []).map(item => item.action)].filter(Boolean).map(action => action.intent)])

/** The deterministic client message id of one ask: the same chat, block,
 *  references (in any order) and hour always give the same id. */
export async function askCid(chatId, fingerprint, refs, at) {
  const sorted = refs.map(ref => `${ref.id}\n${ref.code}`).sort()
  const input = JSON.stringify(['ask-v1', chatId, fingerprint, sorted, Math.floor(at / ASK_PERIOD_MS)])
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(input))
  return `ask-${[...new Uint8Array(digest)].slice(0, 16).map(byte => byte.toString(16).padStart(2, '0')).join('')}`
}

const follows = (node, other) => Boolean(node && other
  && node.compareDocumentPosition(other) & 4 /* Node.DOCUMENT_POSITION_FOLLOWING */)

export function createAppBlockChat(chatId, { send = null, now = () => Date.now() } = {}) {
  const groups = new Map() // key -> Map(id -> { node, superseded })
  const listeners = new Set()
  let refresh = { seq: 0, reason: '' }
  // Per card key: 'sending' until the send settles, then 'asked' until a
  // turn here settles. Presentation only: the message id is what dedupes.
  const pending = new Map()
  let askVersion = 0
  const notify = () => listeners.forEach(listener => listener())
  const changed = () => { askVersion += 1; notify() }
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
    /** One card's ask control: `ready`, `sending` or `asked`. */
    askState(key) { return pending.get(key) || 'ready' },
    /** Send one card's ask (`message` with the id from `fingerprint` and
     *  `refs`) as an owner message, if that card is still the newest of its
     *  key and not already sending. Resolves to whether it was accepted. */
    async ask(key, id, { message, fingerprint, refs }) {
      if (!send || typeof message !== 'string' || !message || !chat.latest(key, id) || pending.get(key) === 'sending') return false
      pending.set(key, 'sending')
      changed()
      let ok = false
      try { ok = (await send(message, { cid: await askCid(chatId, fingerprint, refs, now()) })) === true } catch { ok = false }
      if (ok) pending.set(key, 'asked')
      else pending.delete(key)
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
 *  callback ref for the block's section. `askState()` and `ask({message,
 *  fingerprint, refs})` are this block's card ask (createAppBlockChat). */
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
    askState: () => chat ? chat.askState(key) : 'ready',
    ask: request => chat ? chat.ask(key, id, request) : Promise.resolve(false) }
}

export const appBlockChatKey = block => `${block.app}\n${block.intent}`
