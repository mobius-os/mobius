/**
 * Credits → Recent activity: the wallet's whole history in a fixed-height
 * window on the Credits page, newest first under day headings. History can
 * run to thousands of lines, so it is read 50 at a time and the next page
 * loads as the owner scrolls near the bottom of the window.
 *
 * Pages follow the service's `next_cursor`; a service that does not page yet
 * sends none, and the window simply ends after its first answer. The query is
 * keyed by pairing, so another service's history never shows under this one.
 */
import { useEffect, useRef } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import { apiFetch, jsonOrThrow } from '../../api/client.js'
import { activityDays, activityTime } from '../../lib/creditsWallet.js'
import { ActivityRow } from './WalletSection.jsx'
import './WalletSection.css'

const PAGE = 50
export const ACTIVITY_KEY = 'credits-activity'

async function readPage({ pageParam, signal }) {
  const before = pageParam ? `&before=${encodeURIComponent(pageParam)}` : ''
  return jsonOrThrow(await apiFetch(`/identity/credits/transactions?limit=${PAGE}${before}`, { signal }))
}

/** The Recent activity group around the window, last on the Credits page. */
export function RecentActivity({ children }) {
  return (
    <section className="settings-group" aria-labelledby="settings-credits-activity-title">
      <div className="settings-group__head">
        <div className="settings-group__heading">
          <h2 className="settings-group__title" id="settings-credits-activity-title">Recent activity</h2>
        </div>
      </div>
      {children}
    </section>
  )
}

/**
 * `refreshKey` changes when the balance does (a purchase, a paid run); the
 * window then reads its history again so the new line appears at the top.
 */
export default function ActivityWindow({ pairing = null, refreshKey = null }) {
  const history = useInfiniteQuery({
    queryKey: [ACTIVITY_KEY, pairing],
    queryFn: readPage,
    initialPageParam: null,
    getNextPageParam: last => last?.next_cursor || undefined,
    staleTime: 0,
  })
  const frame = useRef(null)
  const sentinel = useRef(null)
  const { hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage, refetch } = history
  const seen = useRef({ pairing, refreshKey })

  useEffect(() => {
    const last = seen.current
    seen.current = { pairing, refreshKey }
    // A new pairing is a new query that reads on its own; only a changed
    // balance under the same pairing needs the history read again.
    if (last.pairing === pairing && last.refreshKey !== refreshKey) void refetch()
  }, [pairing, refreshKey, refetch])

  // Load the next page a little before the window is scrolled to its end.
  // After a failed page the window waits for Try again: a fresh observer would
  // report the still-visible end at once and retry in a loop.
  useEffect(() => {
    const node = sentinel.current
    if (!node || !hasNextPage || isFetchNextPageError || typeof IntersectionObserver === 'undefined') return undefined
    const watcher = new IntersectionObserver(([seen]) => {
      if (seen.isIntersecting && !isFetchingNextPage) void fetchNextPage()
    }, { root: frame.current, rootMargin: '0px 0px 240px 0px' })
    watcher.observe(node)
    return () => watcher.disconnect()
  }, [hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage])

  let body
  if (history.isPending) {
    body = <p className="settings-item wallet-activity__status" role="status">Loading your activity…</p>
  } else if (history.isError && !history.data) {
    body = (
      <div className="settings-item wallet-activity__status" role="alert">
        <span>Your activity could not be read just now.</span>
        <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => history.refetch()}>Try again</button>
      </div>
    )
  } else {
    const entries = history.data.pages.flatMap(page => page?.transactions || [])
    body = entries.length ? (
      <>
        {activityDays(entries).map(day => (
          <div className="wallet-day" key={day.key} role="group" aria-label={day.label}>
            <h3 className="wallet-day__title">{day.label}</h3>
            <ul className="wallet-activity">
              {day.entries.map(entry => <ActivityRow key={entry.id} entry={entry} when={activityTime(entry.at)} />)}
            </ul>
          </div>
        ))}
        {!isFetchNextPageError && <div ref={sentinel} className="wallet-activity__sentinel" aria-hidden="true" />}
        {isFetchingNextPage && <p className="settings-item wallet-activity__status" role="status">Loading more…</p>}
        {isFetchNextPageError && (
          <div className="settings-item wallet-activity__status" role="alert">
            <span>More activity could not be loaded.</span>
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => fetchNextPage()}>Try again</button>
          </div>
        )}
      </>
    ) : (
      <p className="settings-item wallet-activity__status">No activity yet. Buying credits or using an app that charges shows up here.</p>
    )
  }

  return (
    <div ref={frame} className="settings-list wallet-window" role="region" tabIndex={0} aria-label="Recent activity, newest first">
      {body}
    </div>
  )
}
