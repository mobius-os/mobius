import { useEffect, useMemo, useRef } from 'react'
import { Search } from '@openai/apps-sdk-ui/components/Icon'
import { appQueries } from '../../hooks/queries.js'
import useExpiryCountdowns from '../../hooks/useExpiryCountdowns.js'
import UninstalledTiles from './UninstalledApps.jsx'
import { filterInstalledApps } from './drawerInformationArchitecture.js'
import './AppsDirectory.css'

export default function AppsDirectory({
  empty,
  status = 'success',
  onRetry,
  resultCount,
  query,
  onQueryChange,
  children,
}) {
  const searchRef = useRef(null)

  useEffect(() => {
    // Desktop users can type immediately; on touch devices, opening the app
    // directory must not summon the software keyboard before they ask for it.
    const shouldFocusSearch = window.matchMedia?.('(pointer: fine)').matches
    const frame = shouldFocusSearch
      ? requestAnimationFrame(() => searchRef.current?.focus())
      : null
    return () => {
      if (frame !== null) cancelAnimationFrame(frame)
    }
  }, [])

  // Uninstalled apps still in their recovery window join the grid, grayed out, and follow the same search.
  const { data: removed = [], dataUpdatedAt } = appQueries.uninstalled.useQuery()
  const removedMatches = useMemo(() => filterInstalledApps(removed, query), [removed, query])
  const removedTiles = useExpiryCountdowns(removedMatches, dataUpdatedAt)
  const nothingInstalled = empty && removed.length === 0
  const noMatches = status === 'success' && !nothingInstalled && resultCount + removedTiles.length === 0

  return (
    <section className="apps-directory" aria-label="Installed apps">
      <header className="apps-directory__header">
        <div className="apps-directory__title">
          <h1>Apps</h1>
        </div>
        <label className="apps-directory__search">
          <Search aria-hidden="true" />
          <input
            ref={searchRef}
            type="search"
            value={query}
            placeholder="Search installed apps"
            aria-label="Search installed apps"
            autoComplete="off"
            spellCheck="false"
            onChange={event => onQueryChange(event.target.value)}
          />
        </label>
      </header>

      <div className="apps-directory__scroll">
        {status === 'loading' ? (
          <div className="apps-directory__empty" role="status">
            <p>Loading apps…</p>
          </div>
        ) : status === 'error' ? (
          <div className="apps-directory__empty" role="alert">
            <h2>Apps unavailable</h2>
            <p>Check your connection, then try again.</p>
            <button type="button" onClick={onRetry}>Try again</button>
          </div>
        ) : (
          <>
            <div className="apps-directory__intro">
              <div>
                <h2>Your apps</h2>
                <p>Everything installed in this Möbius workspace.</p>
              </div>
              {query && !nothingInstalled && (
                <span aria-live="polite">
                  {resultCount + removedTiles.length} {resultCount + removedTiles.length === 1 ? 'match' : 'matches'}
                </span>
              )}
            </div>

            {nothingInstalled ? (
              <div className="apps-directory__empty">
                <h2>No installed apps yet</h2>
                <p>Installed apps will appear here as soon as you add one.</p>
              </div>
            ) : noMatches ? (
              <div className="apps-directory__empty" aria-live="polite">
                <h2>No apps found</h2>
                <p>Try a different name or keyword.</p>
              </div>
            ) : (
              <div className="apps-directory__grid">
                {children}
                <UninstalledTiles entries={removedTiles} />
              </div>
            )}
          </>
        )}
      </div>
    </section>
  )
}
