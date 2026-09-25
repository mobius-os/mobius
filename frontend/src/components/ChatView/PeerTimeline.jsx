/* Exact-chat activity shares one read-only timeline and never resumes agent work. */
import { useEffect, useMemo } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import { api, jsonOrThrow } from '../../api/client.js'
import HelperResultCard, { HelperResultGroupCard } from './HelperResultCard.jsx'
import PeerMessageCard from './PeerMessageCard.jsx'
import ActivityStretch from './ActivityStretch.jsx'
import { projectChatActivity } from './chatActivity.js'
import {
  CHAT_ACTIVITY_STALE_TIME,
  chatActivityQueryKey,
  retryChatActivity,
} from './chatActivityQueries.js'
import { groupTimelineRows } from './timelineRowGrouping.js'
import { peerRecordTool, peerTime, foldPeerActivity } from './peerTimeline.js'

export function usePeerTimeline(chatId, messages, enabled, activeTools, activeMirrorIndex = -1) {
  const query = useInfiniteQuery({
    queryKey: chatActivityQueryKey(chatId),
    initialPageParam: null,
    queryFn: async ({ pageParam, signal }) => jsonOrThrow(await api.chats.activity(chatId, { before: pageParam, signal }), 'Chat activity failed:'),
    getNextPageParam: page => page.next_before || undefined,
    enabled, staleTime: CHAT_ACTIVITY_STALE_TIME, retry: retryChatActivity,
  })
  const pages = query.data?.pages
  const events = useMemo(() => [...new Map((pages || []).flatMap(p => p.events).map(event => [event.id, event])).values()], [pages])
  const oldestLoaded = events.length ? Math.min(...events.map(event => peerTime(event.created_at))) : Infinity
  const windowStart = messages[0]?.ts ?? Infinity
  const { hasNextPage, isFetching, isError, fetchNextPage } = query
  useEffect(() => {
    // Include ties at the window boundary: a page may split one timestamp.
    if (enabled && hasNextPage && !isFetching && !isError && oldestLoaded >= windowStart) void fetchNextPage()
  }, [enabled, hasNextPage, isFetching, isError, oldestLoaded, windowStart, fetchNextPage])
  return useMemo(
    () => foldPeerActivity(
      messages,
      projectChatActivity(messages, events, chatId, activeTools),
      chatId,
      activeMirrorIndex,
    ),
    [messages, events, chatId, activeTools, activeMirrorIndex],
  )
}

export function PeerTimelineRows({ notes, chatId, onInternalNav }) {
  return groupTimelineRows(notes).map(group => {
    const groupedHelpers = group.length > 1 && group.every(note => note.type === 'helper_result')
    const groupedPeers = group.length > 1 && group.every(note => note.type === 'peer_message')
    const key = groupedHelpers
      ? `helper-results:${group.map(note => note.activityId || note.id).join(',')}`
      : groupedPeers
      ? `peer-messages:${group.map(note => note.id).join(',')}`
      : group[0].activityId || `peer-${group[0].id}`
    const note = group[0]
    return <li key={key} className="chat__msg chat__msg--assistant chat__msg--peer" data-peer-id={note.type === 'peer_message' ? note.id : undefined} data-activity-id={groupedHelpers ? undefined : note.activityId} data-key={key} tabIndex={-1}>
    <div className="chat__tools">
      {groupedHelpers
        ? <HelperResultGroupCard events={group} chatId={chatId} onInternalNav={onInternalNav} />
        : groupedPeers
        ? <ActivityStretch
            entries={group.map((peer, index) => ({
              idx: `peer-${peer.id || index}`,
              item: {
                ...peerRecordTool(peer, chatId),
                tool_use_id: `peer-${peer.id || index}`,
                peer_records: [peer],
              },
            }))}
            chatId={chatId}
            surfaceKey={key}
            onInternalNav={onInternalNav}
          />
        : note.type === 'helper_result'
        ? <HelperResultCard event={note} chatId={chatId} onInternalNav={onInternalNav} />
        : <PeerMessageCard onInternalNav={onInternalNav} t={peerRecordTool(note, chatId)} chatId={chatId} disclosureKey={`peer-${note.id}`} records={[note]} />}
    </div>
  </li>
  })
}
