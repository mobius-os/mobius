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
   client message id derived from the chat, the block's content and its
   references with their revisions (askCid). The server already treats a
   repeated id as the same message, so a retry, a reload, a second tab or a
   concurrent tap of the same ask about the same state posts it once; a
   changed state (a new `rev`) can be asked about again. Nothing here counts
   or remembers sends for safety: the in-flight set and the receipts only
   say what one ask's button shows, and both belong to that exact ask
   (askIdentity), never to a card or its group. */
import { createContext, useContext, useLayoutEffect, useState, useSyncExternalStore } from 'react'

export const REFRESH_REASONS = Object.freeze(['turn', 'visible'])
/** What makes one block itself, for its asks: its app, intent, title and the
 *  intents it lists and acts on, as the transcript printed them. It is the
 *  block's saved content, not where it occurs: two identical blocks in one
 *  chat share their asks, which is intended. It grants nothing. */
export const blockFingerprint = block => JSON.stringify([block.app, block.intent, block.title,
  (block.items || []).map(item => item.intent), [block.action, ...(block.items || []).map(item => item.action)].filter(Boolean).map(action => action.intent)])

/** One ask's identity in this chat: its block content and its references
 *  with their revisions, in any order. askCid hashes the same inputs. */
export const askIdentity = (fingerprint, refs) => JSON.stringify([fingerprint,
  refs.map(ref => JSON.stringify([ref.id, ref.code, ref.rev])).sort()])

/** The deterministic client message id of one ask: the same chat, block
 *  content and references with their revisions (in any order) always give
 *  the same id, whenever and wherever it is computed. */
export async function askCid(chatId, fingerprint, refs) {
  const sorted = refs.map(ref => JSON.stringify([ref.id, ref.code, ref.rev])).sort()
  const input = JSON.stringify(['ask-v1', chatId, fingerprint, sorted])
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(input))
  return `ask-${[...new Uint8Array(digest)].slice(0, 16).map(byte => byte.toString(16).padStart(2, '0')).join('')}`
}

const follows = (node, other) => Boolean(node && other
  && node.compareDocumentPosition(other) & 4 /* Node.DOCUMENT_POSITION_FOLLOWING */)

export function createAppBlockChat(chatId, { send = null } = {}) {
  const groups = new Map() // key -> Map(id -> { node, superseded })
  const listeners = new Set()
  let refresh = { seq: 0, reason: '' }
  // By ask identity: the asks whose send is in flight, and the answered
  // ones ('asked': posted, 'already': the server already had it) until a
  // turn here settles. `turns` counts settled turns, so an acknowledgement
  // that arrives after a turn settled leaves no receipt behind.
  const inFlight = new Set()
  const receipts = new Map()
  let turns = 0
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
    /** A turn in this chat settled (visible or not): every answered ask is
     *  over, so its control comes back. An ask still in flight stays so;
     *  its late acknowledgement records nothing (see ask). */
    turnSettled() {
      turns += 1
      if (receipts.size) { receipts.clear(); changed() }
    },
    /** One ask's control: `ready`, `sending`, `asked` or `already`, for the
     *  ask `{fingerprint, refs}` (a changed ask starts from `ready`). */
    askState({ fingerprint, refs }) {
      const identity = askIdentity(fingerprint, refs)
      return inFlight.has(identity) ? 'sending' : receipts.get(identity) || 'ready'
    },
    /** Send one ask (`message` with the id from `fingerprint` and `refs`) as
     *  an owner message, if its card is still the newest of its key and the
     *  same ask is not already in flight. Resolves to whether it was
     *  accepted. `send` resolves to true, 'duplicate' (the server already
     *  had this id) or false. */
    async ask(key, id, { message, fingerprint, refs }) {
      const identity = askIdentity(fingerprint, refs)
      if (!send || typeof message !== 'string' || !message || !chat.latest(key, id) || inFlight.has(identity)) return false
      inFlight.add(identity)
      const turn = turns
      changed()
      let result = false
      try { result = await send(message, { cid: await askCid(chatId, fingerprint, refs) }) } catch { result = false }
      inFlight.delete(identity)
      const ok = result === true || result === 'duplicate'
      // A turn that settled while this was in flight may have been its own
      // answer: record nothing, so no expired receipt outlives it.
      if (ok && turn === turns) receipts.set(identity, result === true ? 'asked' : 'already')
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
 *  callback ref for the block's section. `askState({fingerprint, refs})` and
 *  `ask({message, fingerprint, refs})` are this block's card ask
 *  (createAppBlockChat). */
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
    askState: request => chat ? chat.askState(request) : 'ready',
    ask: request => chat ? chat.ask(key, id, request) : Promise.resolve(false) }
}

export const appBlockChatKey = block => `${block.app}\n${block.intent}`
