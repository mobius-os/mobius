/* ChatNetworkInspector presents retained chat-specific mail with explicit routing. */
import { useId, useRef } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import { X, ArrowRotateCw } from '@openai/apps-sdk-ui/components/Icon'
import { api, jsonOrThrow } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { networkSummary } from './ChatAgentNetwork.jsx'
import './ChatUsageInspector.css'
import './ChatNetworkInspector.css'

function timestamp(value) {
  if (!value) return ''
  const date = new Date(/(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

export function NetworkMessage({ message, chatId }) {
  const sent = message.sender_chat_id === chatId
  const audience = message.broadcast
    ? (message.room_kind === 'project' ? 'Project group' : 'Goal group')
    : (message.recipient_chat_id === chatId ? 'This chat' : message.recipient_name || 'Agent')
  return <li className={`cni-message cni-message--${message.kind || 'note'}`}>
    <div className="cni-message__meta">
      <span className="cni-message__kind">{message.kind || 'note'}</span>
      <span className="cni-message__direction">{sent ? 'Sent to ' : 'Received from '}
        <strong>{sent ? audience : message.sender_name || 'Agent'}</strong>
        {message.broadcast && <span> · Broadcast{!sent && ` to ${audience}`}</span>}
      </span>
      <time dateTime={message.created_at}>{timestamp(message.created_at)}</time>
    </div>
    <p className="cni-message__body">{message.body}</p>
  </li>
}

export default function ChatNetworkInspector({ chatId, onClose }) {
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const titleId = useId()
  const query = useInfiniteQuery({
    queryKey: ['chat-network-history', chatId],
    initialPageParam: null,
    queryFn: async ({ pageParam }) => jsonOrThrow(await api.agentCoordination.history(chatId, { before: pageParam }), 'Agent network failed:'),
    getNextPageParam: page => page.next_before || undefined,
    staleTime: 5_000,
    retry: 0,
  })
  useDialogFocus({ containerRef: dialogRef, initialFocusRef: closeRef, onClose })
  const pages = query.data?.pages || []
  const messages = [...new Map(pages.flatMap(page => page.messages).map(message => [message.id, message])).values()]
  return <div className="cui__overlay" role="presentation" onClick={onClose}>
    <section className="cui cni" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} ref={dialogRef} onClick={event => event.stopPropagation()}>
      <header className="cui__head">
        <div><h2 className="cui__title" id={titleId}>Agent network</h2>
          <p className="cui__subtitle">{networkSummary(pages[0])}</p>
        </div>
        <button ref={closeRef} className="cui__close" type="button" aria-label="Close agent network" onClick={onClose}><X width={18} height={18} /></button>
      </header>
      <div className="cui__body">
        <div className="cni-toolbar"><p>Sent by this chat or available to its agent. Newest first.</p>
          <button type="button" className="cui__close" aria-label="Refresh messages" disabled={query.isFetching} onClick={() => query.refetch()}><ArrowRotateCw width={18} height={18} /></button>
        </div>
        {query.isLoading && <p className="cui__state">Loading messages…</p>}
        {query.isError && <div className="cui__state" role="alert"><p>Messages couldn’t load. {messages.length > 0 ? 'Loaded messages remain below.' : 'Please try again.'}</p><button className="cui__retry" type="button" disabled={query.isFetching} onClick={() => query.isFetchNextPageError ? query.fetchNextPage() : query.refetch()}>Try again</button></div>}
        {!query.isLoading && !query.isError && messages.length === 0 && <p className="cui__state">No messages yet. Direct notes and broadcasts available to this chat will appear here.</p>}
        <ol className="cni-messages">{messages.map(message => <NetworkMessage key={message.id} message={message} chatId={chatId} />)}</ol>
        {query.hasNextPage && <button className="cui__retry" type="button" disabled={query.isFetching} onClick={() => query.fetchNextPage()}>{query.isFetchingNextPage ? 'Loading older messages…' : 'Load older messages'}</button>}
        {messages.length > 0 && !query.hasNextPage && <p className="cni-foot">All retained messages shown. Previously removed messages are not recoverable.</p>}
      </div>
    </section>
  </div>
}
