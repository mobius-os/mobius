/* GlobalSearch is the shell command palette and cross-workspace search dialog. */
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
  clearRecentSelections,
  chatSearchOpenTarget,
  chatSearchResultIsCurrent,
  moveSearchSelection,
  pointerPositionChanged,
  readRecentSelections,
  rememberRecentSelection,
  resolveRecentSelections,
  resolvedSearchSelection,
  searchCommands,
  searchInstalledApps,
  visibleChatSearchState,
} from './globalSearchModel.js'
import './GlobalSearch.css'

const SEARCH_DEBOUNCE_MS = 180

function GlobalSearchResult({
  row,
  activeResultIndex,
  onOpen,
  onPointerActivity,
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
    // Some browsers emit pointerenter AND a zero-distance pointermove when the
    // dialog mounts beneath a stationary cursor. The parent compares actual
    // coordinates before treating either event as selection intent.
    onPointerEnter: event => onPointerActivity(index, event),
    onPointerMove: event => onPointerActivity(index, event),
    onFocus: () => onSelect(index),
    onClick: () => onOpen(row),
  }

  if (row.kind === 'command') {
    const command = row.value
    const unavailable = command.enabled === false
    return (
      <button
        {...sharedProps}
        className={`${resultClass} global-search__result--command`}
        aria-disabled={unavailable || undefined}
        title={unavailable ? command.unavailableReason : undefined}
      >
        <span className="global-search__result-icon global-search__command-icon" aria-hidden="true">
          ⌘
        </span>
        <span className="global-search__result-main">
          <span className="global-search__result-title">{command.title}</span>
          <span className="global-search__result-detail">
            {unavailable && command.unavailableReason
              ? command.unavailableReason
              : command.description}
          </span>
        </span>
        <span className="global-search__command-bindings" aria-label={command.shortcutLabels.join(' or ')}>
          {command.shortcutLabels.map(label => <kbd key={label}>{label}</kbd>)}
        </span>
      </button>
    )
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
  const label = active ? 'Close search' : 'Search and commands'
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

export default function GlobalSearch({ commands = [], onClose, onOpenTarget, onRunCommand }) {
  const dialogRef = useRef(null)
  const inputRef = useRef(null)
  const contentRef = useRef(null)
  const chatSearchControllerRef = useRef(null)
  const latestQueryRef = useRef('')
  const [query, setQuery] = useState('')
  const [chatState, setChatState] = useState({
    query: '', status: 'idle', results: [],
  })
  const [recentSelectionRefs, setRecentSelectionRefs] = useState(
    () => readRecentSelections(),
  )
  const [selectionIndex, setSelectionIndex] = useState(0)
  const pointerPositionRef = useRef(null)
  const appsQuery = appQueries.list.useQuery()
  const chatsQuery = chatQueries.list.useQuery()

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
  const commandResults = useMemo(
    () => searchCommands(commands, normalizedQuery).filter(command => command.id !== 'search.open'),
    [commands, normalizedQuery],
  )
  const recentSelectionRows = useMemo(
    () => resolveRecentSelections(
      recentSelectionRefs,
      chatsQuery.data,
      appsQuery.data,
    ),
    [appsQuery.data, chatsQuery.data, recentSelectionRefs],
  )
  const openChat = useCallback((result) => {
    if (!chatSearchResultIsCurrent(result, latestQueryRef.current)) return
    rememberRecentSelection({ kind: 'chat', id: result.id })
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
    rememberRecentSelection({ kind: 'app', id: app.id })
    onClose()
    onOpenTarget?.({ view: 'canvas', app: String(app.id), intent: null })
  }, [onClose, onOpenTarget])

  const openRecentChat = useCallback((chat) => {
    if (!chat?.id) return
    rememberRecentSelection({ kind: 'chat', id: chat.id })
    onClose()
    onOpenTarget?.(chatSearchOpenTarget(chat))
  }, [onClose, onOpenTarget])

  const openCommand = useCallback((command) => {
    if (!command?.id || command.enabled === false) return
    onClose()
    onRunCommand?.(command.id)
  }, [onClose, onRunCommand])

  const resultGroups = useMemo(() => {
    const groups = normalizedQuery
      ? [
          ...(commandResults.length ? [{
            headingId: 'global-search-commands',
            listId: 'global-search-command-results',
            label: 'Commands',
            rows: commandResults.map(command => ({
              kind: 'command', value: command,
            })),
          }] : []),
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
          ...(commandResults.length ? [{
            headingId: 'global-search-commands',
            listId: 'global-search-command-results',
            label: 'Commands',
            rows: commandResults.map(command => ({
              kind: 'command', value: command,
            })),
          }] : []),
          ...(recentSelectionRows.length ? [{
            headingId: 'global-search-recent-selections',
            listId: 'global-search-recent-selection-results',
            label: 'Recent selections',
            clearable: true,
            rows: recentSelectionRows.map(({ kind, value }) => ({
              kind,
              value,
              recent: true,
              matchArea: 'Recent',
            })),
          }] : []),
        ]

    let nextIndex = 0
    return groups.map(group => ({
      ...group,
      rows: group.rows.map(row => ({ ...row, index: nextIndex++ })),
    }))
  }, [appResults, commandResults, normalizedQuery, recentSelectionRows, visibleChats])

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
    if (row?.kind === 'command') openCommand(row.value)
    if (row?.kind === 'app') openApp(row.value)
    if (row?.kind === 'chat' && row.recent) openRecentChat(row.value)
    if (row?.kind === 'chat' && !row.recent) openChat(row.value)
  }, [openApp, openChat, openCommand, openRecentChat])

  const openSelectedResult = useCallback(() => {
    openResult(selectableResults[activeResultIndex])
  }, [activeResultIndex, openResult, selectableResults])

  const clearSelectionHistory = useCallback(() => {
    clearRecentSelections()
    setRecentSelectionRefs([])
    setSelectionIndex(0)
  }, [])

  const handleResultPointerActivity = useCallback((index, event) => {
    const nextPosition = { x: event.clientX, y: event.clientY }
    const moved = pointerPositionChanged(
      pointerPositionRef.current,
      nextPosition,
    )
    pointerPositionRef.current = nextPosition
    if (moved) setSelectionIndex(index)
  }, [])

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
    && commandResults.length === 0
  const loadingRecentSelections = !normalizedQuery
    && resultGroups.length === 0
    && recentSelectionRefs.some(selection => (
      selection.kind === 'app' ? appsQuery.isLoading : chatsQuery.isLoading
    ))

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
            <h2 id="global-search-title" className="global-search__title">Search &amp; commands</h2>
            <p className="global-search__subtitle">Workspace actions, chats, and installed apps</p>
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
            placeholder="Search commands, chats, apps, and app details"
            aria-label="Search commands, chats, apps, and app details"
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
              <h3>{loadingRecentSelections ? 'Loading workspace actions…' : 'No commands are available'}</h3>
              <p>
                {loadingRecentSelections
                  ? 'Commands and the chats and apps you opened through search will appear here.'
                  : 'Search for a command, chat, or app.'}
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
                  <div className="global-search__group-heading">
                    <h3 id={group.headingId}>
                      {group.label} <span>{group.rows.length}</span>
                    </h3>
                    {group.clearable && (
                      <button
                        type="button"
                        className="global-search__clear"
                        onClick={clearSelectionHistory}
                      >
                        Clear
                      </button>
                    )}
                  </div>
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
                          onPointerActivity={handleResultPointerActivity}
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
