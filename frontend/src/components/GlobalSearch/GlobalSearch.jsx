/* GlobalSearch is the keyboard-openable search dialog for chats and installed apps. */
import { createPortal } from 'react-dom'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Chat,
  MagnifyingGlassSearch,
  X,
} from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { appQueries, chatQueries } from '../../hooks/queries.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { requestChatSearchReveal } from '../../lib/chatSearchReveal.js'
import AppIcon from '../AppIcon.jsx'
import {
  SHELL_SHORTCUTS,
  shortcutLabel,
} from '../../lib/keyboardShortcuts.js'
import { searchSnippetPresentation } from '../../lib/searchTermHighlight.js'
import { formatRelativeTime } from '../../lib/relativeTime.js'
import {
  chatSearchOpenTarget,
  chatSearchResultIsCurrent,
  moveSearchSelection,
  recentApps,
  recentChats,
  readLastSearch,
  rememberLastSearch,
  resolvedSearchSelection,
  searchInstalledApps,
  visibleChatSearchState,
} from './globalSearchModel.js'
import './GlobalSearch.css'

const SEARCH_DEBOUNCE_MS = 180
const RECENT_RESULT_LIMIT = 6

function GlobalSearchResult({
  row,
  activeResultIndex,
  onOpen,
  onSelect,
}) {
  const { index } = row
  const selected = activeResultIndex === index
  const resultClass = `global-search__result${
    selected ? ' global-search__result--selected' : ''
  }`
  const sharedProps = {
    id: `global-search-result-${index}`,
    type: 'button',
    role: 'option',
    'aria-selected': selected,
    'data-search-result-index': index,
    className: resultClass,
    onPointerEnter: () => onSelect(index),
    onFocus: () => onSelect(index),
    onClick: () => onOpen(row),
  }

  if (row.kind === 'app') {
    const app = row.value
    return (
      <button {...sharedProps}>
        <AppIcon
          item={app}
          label={app.name}
          className="global-search__result-icon"
        />
        <span className="global-search__result-main">
          <span className="global-search__result-title">{app.name}</span>
          <span className="global-search__result-detail">
            {app.description || app.slug}
          </span>
        </span>
        <span className="global-search__match-kind">{row.matchArea}</span>
      </button>
    )
  }

  const result = row.value
  const lastActiveValue = result.last_active
    || result.activity_at
    || result.updated_at
    || result.created_at
  const lastActive = formatRelativeTime(lastActiveValue)
  return (
    <button {...sharedProps}>
      <span className="global-search__result-icon" aria-hidden="true">
        <Chat width={18} height={18} />
      </span>
      <span className="global-search__result-main">
        <span className="global-search__result-title">
          {result.title || 'Untitled chat'}
        </span>
        {result.snippet && (
          <span className="global-search__result-detail">
            {result.snippetParts.map((part, partIndex) => (
              part.marked
                ? <mark key={partIndex}>{part.text}</mark>
                : <span key={partIndex}>{part.text}</span>
            ))}
          </span>
        )}
      </span>
      <span className="global-search__result-meta">
        <span className="global-search__match-kind">
          {row.recent ? 'Recent' : (result.anchor_key ? 'Conversation' : 'Title')}
        </span>
        {lastActive && (
          <time
            className="global-search__result-time"
            dateTime={lastActiveValue}
            title={`Last active ${new Date(lastActiveValue).toLocaleString()}`}
          >
            {lastActive}
          </time>
        )}
      </span>
    </button>
  )
}

export function GlobalSearchButton({ active = false, buttonRef, onClick }) {
  const shortcut = shortcutLabel(SHELL_SHORTCUTS.openSearch)
  const label = active ? 'Close search' : 'Search chats and apps'
  return (
    <button
      ref={buttonRef}
      type="button"
      className={`global-search-button${active ? ' global-search-button--active' : ''}`}
      aria-label={label}
      aria-expanded={active}
      aria-controls="global-search-dialog"
      title={`${label} (${shortcut})`}
      onClick={onClick}
    >
      <MagnifyingGlassSearch width={19} height={19} aria-hidden="true" />
    </button>
  )
}

export default function GlobalSearch({ onClose, onOpenTarget }) {
  const dialogRef = useRef(null)
  const inputRef = useRef(null)
  const contentRef = useRef(null)
  const chatSearchControllerRef = useRef(null)
  // Reopening restores the owner's last search (see rememberLastSearch), so the
  // in-flight-result guard has to start from that same term rather than '' —
  // otherwise clicking a restored result before the revalidating fetch lands
  // would be discarded as stale.
  const restored = useRef(readLastSearch()).current
  const latestQueryRef = useRef(restored.query.trim())
  const [query, setQuery] = useState(restored.query)
  const [chatState, setChatState] = useState(restored.chatState)
  const [selectionIndex, setSelectionIndex] = useState(0)
  const appsQuery = appQueries.list.useQuery()
  const chatsQuery = chatQueries.list.useQuery()

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: inputRef,
    onClose,
  })

  // A restored term is a starting point, not something to edit around: select
  // it so the next keystroke replaces it, exactly like reopening a browser's
  // find bar. Runs once, after useDialogFocus has moved focus to the input.
  useEffect(() => {
    if (restored.query) inputRef.current?.select()
  }, [restored.query])

  useEffect(() => {
    rememberLastSearch(query, chatState)
  }, [query, chatState])

  useEffect(() => {
    const normalizedQuery = query.trim()
    latestQueryRef.current = normalizedQuery
    chatSearchControllerRef.current?.abort()
    chatSearchControllerRef.current = null
    if (!normalizedQuery) {
      setChatState({ query: '', status: 'idle', results: [] })
      return undefined
    }

    const controller = new AbortController()
    chatSearchControllerRef.current = controller
    // Reopening re-runs the query so a chat renamed, added, or deleted since
    // last time is reflected. Keep the restored results on screen while that
    // happens: blanking them to "Searching chats…" would undo the point of
    // restoring them. A genuinely new term has no settled results to hold, so
    // it still shows the loading state.
    setChatState(previous => (
      previous.query === normalizedQuery && previous.status === 'ready'
        ? previous
        : { query: normalizedQuery, status: 'loading', results: [] }
    ))
    const timer = window.setTimeout(async () => {
      try {
        const response = await api.chats.search(normalizedQuery, {
          signal: controller.signal,
        })
        if (!response.ok) throw new Error(`CHAT_SEARCH_${response.status}`)
        const payload = await response.json()
        if (controller.signal.aborted || chatSearchControllerRef.current !== controller) return
        const results = (Array.isArray(payload) ? payload : []).map(result => {
          const snippet = searchSnippetPresentation(result.snippet)
          return {
            ...result,
            searchQuery: normalizedQuery,
            searchTerms: snippet.terms,
            snippetParts: snippet.parts,
          }
        })
        setChatState({ query: normalizedQuery, status: 'ready', results })
      } catch (error) {
        if (
          error?.name !== 'AbortError'
          && !controller.signal.aborted
          && chatSearchControllerRef.current === controller
        ) {
          setChatState({ query: normalizedQuery, status: 'error', results: [] })
        }
      }
    }, SEARCH_DEBOUNCE_MS)

    return () => {
      window.clearTimeout(timer)
      controller.abort()
      if (chatSearchControllerRef.current === controller) {
        chatSearchControllerRef.current = null
      }
    }
  }, [query])

  const normalizedQuery = query.trim()
  const visibleChats = visibleChatSearchState(chatState, normalizedQuery)
  const appResults = useMemo(
    () => searchInstalledApps(appsQuery.data, normalizedQuery),
    [appsQuery.data, normalizedQuery],
  )
  const initialChats = useMemo(
    () => recentChats(chatsQuery.data, RECENT_RESULT_LIMIT),
    [chatsQuery.data],
  )
  const initialApps = useMemo(
    () => recentApps(appsQuery.data, RECENT_RESULT_LIMIT),
    [appsQuery.data],
  )
  const openChat = useCallback((result) => {
    if (!chatSearchResultIsCurrent(result, latestQueryRef.current)) return
    if (result.anchor_key) {
      requestChatSearchReveal(result.id, {
        anchorKey: result.anchor_key,
        terms: result.searchTerms,
      })
    }
    onClose()
    onOpenTarget?.(chatSearchOpenTarget(result))
  }, [onClose, onOpenTarget])

  const openApp = useCallback((app) => {
    if (!app?.id) return
    onClose()
    onOpenTarget?.({ view: 'canvas', app: String(app.id), intent: null })
  }, [onClose, onOpenTarget])

  const openRecentChat = useCallback((chat) => {
    if (!chat?.id) return
    onClose()
    onOpenTarget?.(chatSearchOpenTarget(chat))
  }, [onClose, onOpenTarget])

  const resultGroups = useMemo(() => {
    const groups = normalizedQuery
      ? [
          ...(appResults.length ? [{
            headingId: 'global-search-apps',
            listId: 'global-search-app-results',
            label: 'Apps',
            rows: appResults.map(({ app, matchArea }) => ({
              kind: 'app', value: app, matchArea,
            })),
          }] : []),
          {
            headingId: 'global-search-chats',
            listId: 'global-search-chat-results',
            label: 'Chats',
            status: visibleChats.status,
            rows: visibleChats.results.map(result => ({
              kind: 'chat', value: result,
            })),
          },
        ]
      : [
          ...(initialChats.length ? [{
            headingId: 'global-search-recent-chats',
            listId: 'global-search-recent-chat-results',
            label: 'Recent chats',
            rows: initialChats.map(chat => ({
              kind: 'chat', value: chat, recent: true,
            })),
          }] : []),
          ...(initialApps.length ? [{
            headingId: 'global-search-recent-apps',
            listId: 'global-search-recent-app-results',
            label: 'Apps',
            rows: initialApps.map(app => ({
              kind: 'app', value: app, matchArea: 'Installed',
            })),
          }] : []),
        ]

    let nextIndex = 0
    return groups.map(group => ({
      ...group,
      rows: group.rows.map(row => ({ ...row, index: nextIndex++ })),
    }))
  }, [appResults, initialApps, initialChats, normalizedQuery, visibleChats])

  const selectableResults = useMemo(
    () => resultGroups.flatMap(group => group.rows),
    [resultGroups],
  )
  const activeResultIndex = resolvedSearchSelection(
    selectionIndex,
    selectableResults.length,
  )
  const resultListIds = resultGroups
    .filter(group => group.rows.length)
    .map(group => group.listId)
    .join(' ')

  const openResult = useCallback((row) => {
    if (row?.kind === 'app') openApp(row.value)
    if (row?.kind === 'chat' && row.recent) openRecentChat(row.value)
    if (row?.kind === 'chat' && !row.recent) openChat(row.value)
  }, [openApp, openChat, openRecentChat])

  const openSelectedResult = useCallback(() => {
    openResult(selectableResults[activeResultIndex])
  }, [activeResultIndex, openResult, selectableResults])

  const handleSearchKeyDown = useCallback((event) => {
    if (event.isComposing || event.metaKey || event.ctrlKey || event.altKey) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (!selectableResults.length) return
      event.preventDefault()
      setSelectionIndex(current => (
        moveSearchSelection(current, event.key, selectableResults.length)
      ))
      return
    }
    if (event.key === 'Enter' && activeResultIndex !== -1) {
      event.preventDefault()
      openSelectedResult()
    }
  }, [activeResultIndex, openSelectedResult, selectableResults.length])

  useEffect(() => {
    if (activeResultIndex === -1) return
    dialogRef.current
      ?.querySelector(`[data-search-result-index="${activeResultIndex}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [activeResultIndex])

  const noResults = normalizedQuery
    && visibleChats.status === 'ready'
    && visibleChats.results.length === 0
    && appResults.length === 0
  const loadingInitialResults = !normalizedQuery
    && resultGroups.length === 0
    && (appsQuery.isLoading || chatsQuery.isLoading)

  return createPortal(
    <div
      className="global-search__overlay"
      role="presentation"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        id="global-search-dialog"
        ref={dialogRef}
        className="global-search"
        role="dialog"
        aria-modal="true"
        aria-labelledby="global-search-title"
      >
        <header className="global-search__header">
          <div>
            <h2 id="global-search-title" className="global-search__title">Search</h2>
            <p className="global-search__subtitle">Chats and installed apps</p>
          </div>
          <button
            type="button"
            className="global-search__close"
            aria-label="Close search"
            onClick={onClose}
          >
            <X width={20} height={20} aria-hidden="true" />
          </button>
        </header>

        <label className="global-search__input-wrap">
          <MagnifyingGlassSearch width={21} height={21} aria-hidden="true" />
          <input
            ref={inputRef}
            type="search"
            value={query}
            onChange={event => {
              setQuery(event.target.value)
              setSelectionIndex(0)
              if (contentRef.current) contentRef.current.scrollTop = 0
            }}
            onKeyDown={handleSearchKeyDown}
            placeholder="Search chats, apps, and app details"
            aria-label="Search chats, apps, and app details"
            role="combobox"
            aria-autocomplete="list"
            aria-controls={resultListIds || undefined}
            aria-expanded={selectableResults.length > 0}
            aria-activedescendant={
              activeResultIndex === -1
                ? undefined
                : `global-search-result-${activeResultIndex}`
            }
            autoComplete="off"
            spellCheck="false"
          />
          <kbd>{shortcutLabel(SHELL_SHORTCUTS.openSearch)}</kbd>
        </label>

        <div
          ref={contentRef}
          className="global-search__content"
          aria-live="polite"
        >
          {!normalizedQuery && resultGroups.length === 0 && (
            <div className="global-search__empty">
              <span className="global-search__empty-icon" aria-hidden="true">
                <MagnifyingGlassSearch width={30} height={30} />
              </span>
              <h3>{loadingInitialResults ? 'Loading recent items…' : 'Nothing here yet'}</h3>
              <p>
                {loadingInitialResults
                  ? 'Your recent chats and installed apps will appear here.'
                  : 'Start a chat or install an app, then use ⌘K to jump back to it.'}
              </p>
            </div>
          )}

          {resultGroups.length > 0 && (
            <div className="global-search__groups">
              {resultGroups.map(group => (
                <section
                  key={group.listId}
                  className="global-search__group"
                  aria-labelledby={group.headingId}
                >
                  <h3 id={group.headingId}>
                    {group.label} <span>{group.rows.length}</span>
                  </h3>
                  {group.status === 'loading' && (
                    <p className="global-search__status" role="status">Searching chats…</p>
                  )}
                  {group.status === 'error' && (
                    <p className="global-search__status global-search__status--error" role="alert">
                      Chat search is unavailable right now. App results still work.
                    </p>
                  )}
                  {group.rows.length > 0 && (
                    <div
                      id={group.listId}
                      className="global-search__results"
                      role="listbox"
                      aria-labelledby={group.headingId}
                    >
                      {group.rows.map(row => (
                        <GlobalSearchResult
                          key={`${row.kind}-${row.value.id}`}
                          row={row}
                          activeResultIndex={activeResultIndex}
                          onOpen={openResult}
                          onSelect={setSelectionIndex}
                        />
                      ))}
                    </div>
                  )}
                </section>
              ))}

              {noResults && (
                <div className="global-search__empty global-search__empty--results">
                  <h3>No matches</h3>
                  <p>Try a shorter phrase or an app detail such as “offline”, “schedule”, or a skill name.</p>
                </div>
              )}
            </div>
          )}
        </div>

      </div>
    </div>,
    document.body,
  )
}
