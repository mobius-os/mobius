/* Host side of the optional transcript block-session channel.

   A generation names one loaded frame document. Capability negotiation,
   app-reported state and owner events all belong to exactly one generation:
   a reload or version promotion starts a new one, and nothing negotiated by
   an earlier document is reused. Generations grow across every canvas on the
   page, so a stale value, even from a remounted canvas, can never match again. */

let latestGeneration = 0

export function createBlockDocuments() {
  const documents = new Map()
  return {
    /** A frame's onLoad: always a new document, even for the same version. */
    load(version) {
      const document = { generation: ++latestGeneration, supported: null }
      documents.set(version, document)
      return document
    },
    /** A frame-mounted report counts only if it names this exact document
     *  generation: a reload keeps the iframe's window, so the sender alone
     *  cannot tell an old document's queued report from its successor's. */
    mounted(version, message) {
      const document = documents.get(version)
      if (!document || message?.blockGeneration !== document.generation) return null
      document.supported = message.supportsAppBlocks === true
      return document
    },
    get(version) { return documents.get(version) || null },
    delete(version) { documents.delete(version) },
  }
}

/** The session envelope a document receives, stamped with its generation. */
export function blockSessionEnvelope(session, generation) {
  if (!session || !Number.isSafeInteger(generation)) return null
  return { sessionId: session.sessionId, generation, actions: session.actions,
    items: Array.isArray(session.items) ? session.items : [],
    checkpoint: session.checkpoint ?? null, retain: session.retain === true,
    recoveryError: session.recoveryError ?? null }
}

/** Whether app-reported state may reach the block host: it must come from
 *  the live supporting document and name that document's exact generation.
 *  Missing, older and future generations are dropped, never relabelled. */
export function acceptsBlockState({ message, session, document, isLive }) {
  return Boolean(isLive && session && document?.supported === true
    && message?.sessionId === session.sessionId && message.generation === document.generation)
}

/** AppCanvas's app-block-state route: forward only an accepted report, with
 *  the generation the frame itself named. */
export function forwardBlockState({ message, session, document, isLive, onState }) {
  if (!acceptsBlockState({ message, session, document, isLive })) return false
  onState?.(message, { generation: message.generation })
  return true
}

/** The owner event to post to the live document, once per nonce. */
export function blockEventDelivery({ session, event, document, sentNonce }) {
  if (!session || !event || document?.supported !== true) return null
  if (event.sessionId !== session.sessionId || event.generation !== document.generation) return null
  if (event.nonce === sentNonce) return null
  return { type: 'moebius:app-block-action', sessionId: event.sessionId, generation: event.generation,
    key: event.key, event: event.event, nonce: event.nonce }
}

/** A host request is honoured only from the live document. A transcript or
 *  passive-session canvas must also be visible: it mounts without an owner
 *  gesture. An ordinary shell app keeps its background requests (for example
 *  chat status or a projects list) while another view is on screen. */
export function hostRequestAllowed({ sourceVersion, liveVersion, visible, transcript }) {
  return sourceVersion === liveVersion && (!transcript || visible === true)
}
