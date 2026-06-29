import { useLayoutEffect, useRef } from 'react'

/**
 * Hook that decides whether the next persisted-message fetch should
 * REPLACE the existing last message (bridge an in-flight turn whose
 * partial we kept on mount) or APPEND a fresh assistant message
 * (a brand-new turn since mount).
 *
 * The decision is captured ONCE on mount as a ts (the unique
 * per-message timestamp persisted with every message) and then read
 * by ChatView's promoteStreamToMessages on each promote. After the
 * first promote calls markBridged(), subsequent promotes always
 * append.
 *
 * Why ts-based, not role-based: messages have NO id field
 * (models.py:31 stores the messages array as a JSON column with
 * role/content/ts/blocks; routes/chats_stream.py:157-161 builds
 * messages with just those keys). The earlier role-based check
 * ("last message is assistant") regressed when the parallel-agent
 * commit be32e58 started landing errors as the LAST message in a
 * chat — the assistant-role gate would still fire, bridging an
 * error message instead of appending a fresh assistant turn.
 * ts-based gating is stable: the kept-partial has a specific ts,
 * and any other last-message-ts (including error/system messages
 * persisted after mount) deterministically falls through to APPEND.
 *
 * @param {object} args
 * @param {boolean} args.runningAtMount  data.running from the
 *   initial /chats/{id} fetch — true iff the agent was mid-turn
 *   when the user opened the chat.
 * @param {{ts: number, role: string} | null} args.lastMsgAtMount
 *   The last persisted message at the moment of mount, or null
 *   when the chat had no messages.
 *
 * @returns {{
 *   shouldBridge: (currentLastMsg: {ts?: number} | null | undefined) => boolean,
 *   findBridgeIndex: (messages: Array<{ts?: number, role?: string}> | null | undefined) => number,
 *   markBridged: () => void,
 * }}
 */
export default function useBridgePartial({ runningAtMount, lastMsgAtMount }) {
  // Captured at most ONCE per hook instance, the first time the
  // arguments resolve to a "yes, bridge" state (running=true AND
  // last message is an assistant message with a real ts). After
  // that the captured ts is sticky — subsequent re-renders with
  // different args don't re-arm or clear the gate.
  //
  // The "at-most-once" framing matters because the inputs are
  // populated by an async fetch in ChatView.jsx. The hook may
  // render several times with runningAtMount=false / lastMsg=null
  // before the fetch lands; only the first valid set captures.
  // bridgedRef is the second one-shot — once markBridged() fires,
  // no future render flips back to true.
  const keptPartialTsRef = useRef(null)
  const capturedRef = useRef(false)
  const bridgedRef = useRef(false)

  // Capture the partial-to-bridge AFTER render commits (not in the
  // render body). React's rules forbid render-phase side effects;
  // useLayoutEffect runs synchronously after commit but before
  // paint, so the captured value is ready before any callback
  // (onStreamEnd, promoteStreamToMessages) reads `shouldBridge`.
  // The capturedRef one-shot ensures subsequent renders with the
  // same or new inputs don't re-arm.
  useLayoutEffect(() => {
    if (capturedRef.current) return
    if (!runningAtMount) return
    if (!lastMsgAtMount) return
    if (lastMsgAtMount.role !== 'assistant') return
    if (lastMsgAtMount.ts == null) return
    capturedRef.current = true
    keptPartialTsRef.current = lastMsgAtMount.ts
  }, [runningAtMount, lastMsgAtMount])

  function candidateTs() {
    if (keptPartialTsRef.current != null) return keptPartialTsRef.current
    // Render-time bridge candidate. The layout-effect capture above is still
    // the sticky lifecycle owner, but render needs to suppress the cached DB
    // partial on the FIRST paint after navigating back to a running chat.
    // Waiting for a later render shows the persisted partial and the cached
    // stream snapshot side-by-side. This derivation is pure (no ref writes),
    // so it is safe during render and still lets markBridged() retire it.
    if (!runningAtMount) return null
    if (!lastMsgAtMount) return null
    if (lastMsgAtMount.role !== 'assistant') return null
    return lastMsgAtMount.ts ?? null
  }

  function shouldBridge(currentLastMsg) {
    if (bridgedRef.current) return false
    const ts = candidateTs()
    if (ts == null) return false
    if (!currentLastMsg) return false
    return currentLastMsg.ts === ts
  }

  function findBridgeIndex(messages) {
    if (bridgedRef.current) return -1
    const ts = candidateTs()
    if (ts == null) return -1
    if (!Array.isArray(messages)) return -1
    return messages.findIndex(
      m => m?.role === 'assistant' && m.ts === ts
    )
  }

  function markBridged() {
    bridgedRef.current = true
  }

  return { shouldBridge, findBridgeIndex, markBridged }
}
