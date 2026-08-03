/**
 * The stable identity of a user message. `cid` is the canonical identity
 * (React key, DOM pin target `data-cid`, queue cancel key, force-steer
 * selection). Current clients mint it; card-221 backfilled `cid=legacy-<ts>`
 * onto every legacy row, so post-migration every user row carries an explicit
 * cid and no read-time derivation is needed (chat_writer.cid_of matches). `ts`
 * is display/ordering metadata only. Returns null for a row with no cid.
 */
export function cidOf(msg) {
  if (!msg) return null
  return msg.cid || null
}
