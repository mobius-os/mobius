/* Inline peer mail uses the existing owner-scoped mailbox and disclosure, never a model inbox poll. */
import { useEffect, useMemo } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import { api, jsonOrThrow } from '../../api/client.js'
import PeerMessageCard from './PeerMessageCard.jsx'
import { peerRecordTool, peerTime, projectPeerTimeline } from './peerTimeline.js'

export function usePeerTimeline(chatId, messages, enabled, activeTools) {
  const query = useInfiniteQuery({
    queryKey: ['chat-network-history', chatId],
    initialPageParam: null,
    queryFn: async ({ pageParam }) => jsonOrThrow(await api.agentCoordination.history(chatId, { before: pageParam }), 'Agent messages failed:'),
    getNextPageParam: page => page.next_before || undefined,
    enabled, staleTime: 5000, retry: false,
  })
  const pages = query.data?.pages
  const history = useMemo(() => [...new Map((pages || []).flatMap(p => p.messages).map(m => [m.id, m])).values()], [pages])
  const oldestLoaded = history.length ? Math.min(...history.map(m => peerTime(m.created_at))) : Infinity
  const windowStart = messages[0]?.ts ?? Infinity
  const { hasNextPage, isFetching, isError, fetchNextPage } = query
  useEffect(() => {
    // Page only far enough to cover the visible transcript, not all chat history.
    if (enabled && hasNextPage && !isFetching && !isError && oldestLoaded > windowStart) void fetchNextPage()
  }, [enabled, hasNextPage, isFetching, isError, oldestLoaded, windowStart, fetchNextPage])
  const projection = useMemo(() => projectPeerTimeline(messages, history, chatId, activeTools), [messages, history, chatId, activeTools])
  return { ...projection, error: query.isError, retry: query.refetch }
}

export function PeerTimelineRows({ notes, chatId }) {
  return notes?.map(note => <li key={`peer-${note.id}`} className="chat__msg chat__msg--assistant chat__msg--peer" data-peer-id={note.id} data-key={`peer-${note.id}`} tabIndex={-1}>
    <div className="chat__tools">
      <PeerMessageCard t={peerRecordTool(note, chatId)} chatId={chatId} disclosureKey={`peer-${note.id}`} records={[note]} />
    </div>
  </li>)
}
