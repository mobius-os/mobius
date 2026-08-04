/* GlobalSearch is the full-screen, keyboard-openable search surface for chats and installed apps. */
import { createPortal } from 'react-dom'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Chat,
  Grid,
  MagnifyingGlassSearch,
  X,
} from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { requestChatSearchReveal } from '../../lib/chatSearchReveal.js'
import {
  SHELL_SHORTCUTS,
  shortcutLabel,
} from '../../lib/keyboardShortcuts.js'
import { searchSnippetPresentation } from '../../lib/searchTermHighlight.js'
import {
  chatSearchOpenTarget,
  chatSearchResultIsCurrent,
  searchInstalledApps,
  visibleChatSearchState,
} from './globalSearchModel.js'
import './GlobalSearch.css'

const SEARCH_DEBOUNCE_MS = 180

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
  const chatSearchControllerRef = useRef(null)
  const latestQueryRef = useRef('')
  const [query, setQuery] = useState('')
  const [chatState, setChatState] = useState({
    query: '', status: 'idle', results: [],
  })
  const appsQuery = appQueries.list.useQuery()

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: inputRef,
    onClose,
  })

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
    setChatState({ query: normalizedQuery, status: 'loading', results: [] })
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

  const noResults = normalizedQuery
    && visibleChats.status === 'ready'
    && visibleChats.results.length === 0
    && appResults.length === 0

  return createPortal(
    <div
      id="global-search-dialog"
      ref={dialogRef}
      className="global-search__overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="global-search-title"
    >
      <div className="global-search">
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
            onChange={event => setQuery(event.target.value)}
            placeholder="Search chats, apps, and app details"
            aria-label="Search chats, apps, and app details"
            autoComplete="off"
            spellCheck="false"
          />
          <kbd>{shortcutLabel(SHELL_SHORTCUTS.openSearch)}</kbd>
        </label>

        <div className="global-search__content" aria-live="polite">
          {!normalizedQuery && (
            <div className="global-search__empty">
              <span className="global-search__empty-icon" aria-hidden="true">
                <MagnifyingGlassSearch width={30} height={30} />
              </span>
              <h3>Find anything you’ve worked on</h3>
              <p>Search chat titles and conversation text, plus app names and app details.</p>
            </div>
          )}

          {normalizedQuery && (
            <div className="global-search__groups">
              {appResults.length > 0 && (
                <section className="global-search__group" aria-labelledby="global-search-apps">
                  <h3 id="global-search-apps">Apps <span>{appResults.length}</span></h3>
                  <div className="global-search__results">
                    {appResults.map(({ app, matchArea }) => (
                      <button
                        key={app.id}
                        type="button"
                        className="global-search__result"
                        onClick={() => openApp(app)}
                      >
                        <span className="global-search__result-icon" aria-hidden="true">
                          <Grid width={18} height={18} />
                        </span>
                        <span className="global-search__result-main">
                          <span className="global-search__result-title">{app.name}</span>
                          <span className="global-search__result-detail">
                            {app.description || app.slug}
                          </span>
                        </span>
                        <span className="global-search__match-kind">{matchArea}</span>
                      </button>
                    ))}
                  </div>
                </section>
              )}

              <section className="global-search__group" aria-labelledby="global-search-chats">
                <h3 id="global-search-chats">
                  Chats
                  {visibleChats.status === 'ready' && <span>{visibleChats.results.length}</span>}
                </h3>
                {visibleChats.status === 'loading' && (
                  <p className="global-search__status" role="status">Searching chats…</p>
                )}
                {visibleChats.status === 'error' && (
                  <p className="global-search__status global-search__status--error" role="alert">
                    Chat search is unavailable right now. App results still work.
                  </p>
                )}
                {visibleChats.results.length > 0 && (
                  <div className="global-search__results">
                    {visibleChats.results.map(result => (
                      <button
                        key={result.id}
                        type="button"
                        className="global-search__result"
                        onClick={() => openChat(result)}
                      >
                        <span className="global-search__result-icon" aria-hidden="true">
                          <Chat width={18} height={18} />
                        </span>
                        <span className="global-search__result-main">
                          <span className="global-search__result-title">
                            {result.title || 'Untitled chat'}
                          </span>
                          {result.snippet && (
                            <span className="global-search__result-detail">
                              {result.snippetParts.map((part, index) => (
                                part.marked
                                  ? <mark key={index}>{part.text}</mark>
                                  : <span key={index}>{part.text}</span>
                              ))}
                            </span>
                          )}
                        </span>
                        <span className="global-search__match-kind">
                          {result.anchor_key ? 'Conversation' : 'Title'}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </section>

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
