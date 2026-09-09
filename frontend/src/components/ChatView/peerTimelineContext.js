/* Share receipt metadata with tool disclosures without fetching per tool. */
import { createContext, useContext } from "react"
export const PeerTimelineContext = createContext(null)
export function usePeerTimelineRecord(toolId) {
  return useContext(PeerTimelineContext)?.tools.get(toolId)
}
