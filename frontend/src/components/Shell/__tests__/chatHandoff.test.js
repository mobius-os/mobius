import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
const shellCss = readFileSync(new URL('../Shell.css', import.meta.url), 'utf8')
const paneChatView = readFileSync(new URL('../PaneChatView.jsx', import.meta.url), 'utf8')
const drawer = readFileSync(new URL('../../Drawer/Drawer.jsx', import.meta.url), 'utf8')
const drawerCss = readFileSync(new URL('../../Drawer/Drawer.css', import.meta.url), 'utf8')
const indexCss = readFileSync(new URL('../../../index.css', import.meta.url), 'utf8')
const chatSurfaceModel = readFileSync(new URL('../chatSurfaceModel.js', import.meta.url), 'utf8')
const workspaceChrome = readFileSync(new URL('../WorkspaceChrome.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../../ChatView/ChatView.jsx', import.meta.url), 'utf8')
const scrollMode = readFileSync(new URL('../../ChatView/useScrollMode.js', import.meta.url), 'utf8')
const detailCache = readFileSync(new URL('../../../lib/chatDetailCache.js', import.meta.url), 'utf8')
const searchTermHighlight = readFileSync(new URL('../../../lib/searchTermHighlight.js', import.meta.url), 'utf8')
const apiClient = readFileSync(new URL('../../../api/client.js', import.meta.url), 'utf8')

function ruleBody(selector, source = shellCss) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return source.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\}`))?.[1] || ''
}

test('chat display readiness admits only coordinate-complete cached transcripts', () => {
  assert.match(
    detailCache,
    /function chatCacheEntryState\([\s\S]*cached\?\.restorationWindowComplete !== true[\s\S]*messageKey\(message, baseOffset \+ index\)[\s\S]*if \(savedAnchorHasNestedPart\) return 'validating'[\s\S]*cached\.running[\s\S]*'stream-catchup' : 'paintable'/,
    'only canonical caches containing the durable row may paint or validate',
  )
  assert.match(chatView,
    /const initialSavedAnchorKey = savedReadingAnchorKey\(chatId\)[\s\S]*const initialCacheEntryState = chatCacheEntryState\([\s\S]*savedReadingAnchorHasNestedPart\(chatId\)[\s\S]*initialCacheEntryState === 'missing'[\s\S]*'cache-validating'/,
    'mount paints a safe cache or mounts an exact-part cache behind validation')
  assert.match(chatView,
    /const activationCacheEntryState = chatCacheEntryState\([\s\S]*setLoading\(activationCacheEntryState === 'missing'\)[\s\S]*activationCacheEntryState === 'validating'[\s\S]*'cache-validating'/,
    'a retained chat revalidates cache coverage on every visible activation')
  assert.match(scrollMode,
    /initialEntryPhaseRef\.current !== 'cached'[\s\S]*initialEntryPhaseRef\.current !== 'ready'[\s\S]*forceRevealRef/,
    'the reveal deadline admits only caller-validated cache or authoritative history')
  assert.match(
    chatView,
    /const transcriptPaintable = \([\s\S]*initialEntryPhase === 'cached' \|\| initialEntryPhase === 'ready'[\s\S]*\) && revealed[\s\S]*const displayReady = activationSettled[\s\S]*&& !loading[\s\S]*&& \(transcriptPaintable \|\| showEmpty \|\| showLoadError\)/,
    'a coordinate-complete cache can paint only after this activation confirms its runtime state',
  )
  assert.match(chatView, /useLayoutEffect\(\(\) => \{[\s\S]*onDisplayReady\?\.\(chatId\)/,
    'ChatView must report layout readiness before its transcript can be promoted')
  assert.match(
    paneChatView,
    /scheduleAfterBrowserPaint\(\s*\(\) => onDisplayReady\(paneId, readyChatId, focusedPresentation\),\s*\)/,
    'the pane boundary must prepare one real destination paint before promotion',
  )
  assert.match(
    paneChatView,
    /displayReadyCancelRef\.current\(\)[\s\S]*useEffect\(\(\) => \(\) => displayReadyCancelRef\.current\(\), \[\]\)/,
    'a superseded or unmounted staging chat must cancel its pending paint handoff',
  )
  assert.match(chatView,
    /if \(displayReady\) onDisplayReady\?\.\(chatId\)[\s\S]*\}, \[chatId, displayReady, onDisplayReady\]\)/,
    'an already-ready chat must re-announce when a cross-pane move changes its handoff owner')
  assert.doesNotMatch(chatView, /onDisplayReadyRef/,
    'the callback dependency is the owner-change signal; a parallel mutable ref would obscure it')
  assert.doesNotMatch(
    chatView,
    /scrollEl\.addEventListener\(['"](?:load|error)['"], requestRevealOnQuiet/,
    'reserved image frames must not extend chat entry while their bytes resolve',
  )
  assert.doesNotMatch(
    chatView,
    /new MutationObserver\(requestRevealOnQuiet\)/,
    'DOM churn without a geometry change must not extend the reveal quiet window',
  )
})

test('activation holds an unchanged running transcript until stream catch-up', () => {
  const initialLoad = chatView.match(
    /const loadActivation = async \(\) => \{[\s\S]*?\n    loadActivation\(\)/,
  )?.[0] || ''
  assert.match(
    initialLoad,
    /cacheCoversSavedAnchor && typeof activationCache\?\.updated_at[\s\S]*\/runtime`[\s\S]*const latestCache = queryClient\.getQueryData\(queryKey\)[\s\S]*chatSnapshotMatchesRuntime\(latestCache, runtime\)[\s\S]*detailCache = latestCache[\s\S]*reused = true/,
    'an unchanged row version reuses the newest complete cache, never its captured predecessor',
  )
  assert.match(
    initialLoad,
    /if \(!reused\) \{[\s\S]*\/chats\/\$\{chatId\}\?limit=20&compact=1/,
    'missing or changed versions must fail closed to the full detail route',
  )
  assert.match(
    initialLoad,
    /if \(reused\) \{[\s\S]*updateChatRuntimeCache[\s\S]*applyMessagesToView\(msgs, detailCache\.offset\)[\s\S]*settleRuntime\(runtime, msgs\)[\s\S]*return/,
    'the fast path must reconcile a retained hidden owner before revealing it',
  )
  assert.match(chatView,
    /activationAnchorKey = searchAnchorKey \|\| savedAnchorKey/,
    'search navigation takes precedence over the saved reading row for one activation')
  assert.match(chatView,
    /reconcileChatSearchActivation\([\s\S]*const searchRevealConsumed[\s\S]*if \(!searchReveal \|\| searchRevealConsumed \|\| !displayReady\) return[\s\S]*consumeChatSearchActivation\([\s\S]*clearChatSearchReveal/,
    'consumption stays latched to the searched activation instead of reloading its saved anchor')
  assert.match(chatView,
    /highlightSearchTerms\(row, searchReveal\.terms\)[\s\S]*row\.focus\(\{ preventScroll: true \}\)[\s\S]*revealAnchor\(canonicalKey, 96, highlight\.firstRange\)/,
    'a validated visible destination focuses and positions the exact marked word')
  assert.doesNotMatch(searchTermHighlight, /replaceWith\(|createElement\(['"]mark['"]\)/,
    'search emphasis must never rewrite React-owned transcript text')
  assert.match(initialLoad,
    /anchorParam = activationAnchorKey[\s\S]*&anchor=\$\{encodeURIComponent\(activationAnchorKey\)\}/,
    'every authoritative return read must contain the exact saved or searched row')
  assert.match(chatView,
    /messageMatchesKey\(message, baseOffset \+ index, activationAnchorKey\)[\s\S]*!searchActivation[\s\S]*remapSavedReadingAnchor/,
    'cache and server rows must resolve every durable alias before canonicalizing it')
  assert.match(initialLoad,
    /runtime\.requested_anchor_found === false[\s\S]*if \(runtimeAnchorMatch\)[\s\S]*CHAT_READING_ANCHOR_NOT_FOUND[\s\S]*retireSavedReadingPosition\(chatId\)[\s\S]*anchorRetired = true/,
    'only an authoritative absent row retires the saved coordinate')
  assert.match(initialLoad,
    /if \(activationCache && cacheCoversSavedAnchor && !anchorRetired\) \{[\s\S]*applyMessagesToView\(refreshed\.messages, refreshed\.offset\)[\s\S]*settleRuntime\(runtime, refreshed\.messages\)[\s\S]*return[\s\S]*const renderFrames = coldTranscriptRenderFrames/,
    'a warm version mismatch must settle atomically before the cold prefix scheduler')
  assert.match(chatView,
    /cacheIsSafeFallback[\s\S]*CHAT_READING_ANCHOR_NOT_FOUND[\s\S]*applyMessagesToView\(\[\], 0\)[\s\S]*setLoadError\(!cacheIsSafeFallback\)/,
    'an incomplete or contradictory cache must be cleared before the error surface paints')
  assert.match(scrollMode,
    /mode\?\.kind !== 'INITIAL'[\s\S]*phase === 'cache-validating' && !resolved[\s\S]*action: 'wait'[\s\S]*initialEntryPhaseRef\.current === 'cache-validating'[\s\S]*onCachedCoordinateReady\?\.\(\)/,
    'the scroll controller admits a nested cache only after the exact DOM part resolves')
  assert.match(scrollMode,
    /savedLocationUnresolvedRef\.current[\s\S]*Object\.hasOwn\(_scrollModes, chatId\)/,
    'an activation error before ready must not erase the unconsumed saved coordinate')
  assert.match(
    chatView,
    /setInitialEntryPhase\(attachesToStream \? 'stream-catchup' : 'ready'\)[\s\S]*if \(running\) \{[\s\S]*connectToStream\(false\)/,
    'a running persisted frame remains gated until stream catch-up commits',
  )
  assert.match(
    chatView,
    /const \[activationSettled, setActivationSettled\] = useState\(false\)[\s\S]*if \(hidden\) return[\s\S]*setActivationSettled\(false\)[\s\S]*const settleRuntime[\s\S]*setActivationSettled\(true\)[\s\S]*const displayReady = activationSettled/,
    'a cached idle verdict must not publish before this activation learns that the chat is running',
  )
  assert.match(
    initialLoad,
    /serverSnapshotBehindLocal\(msgs, messagesRef\.current\)[\s\S]*optimisticHandoffWindow\([\s\S]*messagesRef\.current,[\s\S]*offsetRef\.current/,
    'an optimistic handoff must select between the concurrent cache and mounted transcript',
  )
  assert.doesNotMatch(initialLoad, /existing\?\.messages \|\| messagesRef\.current/,
    'a truthy empty cache must not erase the selected transcript during a cross-owner handoff')
})

test('a staging chat cannot leave the outgoing transcript held on a wedged request', () => {
  assert.match(chatView, /const CHAT_FETCH_TIMEOUT_MS = 15000/)
  assert.match(
    chatView,
    /const requestJson = async \(path, label\) => \{[\s\S]*apiFetch\(path, \{\s*timeoutMs: CHAT_FETCH_TIMEOUT_MS,\s*signal: initialLoadController\.signal,\s*\}\)/,
    'both version and detail reads must share the bounded activation deadline',
  )
  assert.match(chatView, /initialLoadController\.abort\(\)/,
    'hiding or unmounting a staging chat must release its request immediately')
  assert.match(apiClient, /AbortSignal\.any\(\[signal, ctrl\.signal\]\)/,
    'apiFetch must compose lifecycle cancellation with its deadline')
  assert.match(apiClient, /error\.name = 'TimeoutError'\s*ctrl\.abort\(error\)/,
    'a deadline remains distinguishable from routine lifecycle cancellation')
  assert.match(apiClient, /if \(error\?\.name !== 'AbortError'\) void verifyConnectivity\(\)/,
    'switching panes must not trigger a redundant connectivity probe')
})

test('each pane holds one outgoing chat over one staging chat', () => {
  assert.match(chatSurfaceModel, /chatId: previousId,[\s\S]*role: 'held'/,
    'the transition keeps only the last painted chat in its pane')
  assert.match(chatSurfaceModel, /role: transitioning \? 'staging' : 'active'/,
    'the destination stages only while a different painted chat exists')
  assert.match(
    shell,
    /paneModel\.activeKeyForOwner\(workspaceStateRef\.current\.ws, paneKey\) !== `chat:\$\{id\}`/,
    'late readiness must be validated against either a real pane or the synthetic single owner',
  )
  // A held/staging chat is inert (the takeover is PAINTING OR not the active role);
  // the condition now also folds in a leaving pane during the exit beat (INV 9), so
  // match the leading clause rather than the exact full expression. The takeover
  // gate is the EFFECTIVE-mode `settingsOverlay` (finding F3), not the committed one.
  assert.match(shell, /inert=\{!surfaceVisible \|\| settingsOverlay \|\| role !== 'active'/,
    'neither the held nor staging chat may accept interaction')
  assert.match(shell, /composerRequest=\{role === 'active' && surfaceVisible \? composerRequest : null\}/,
    'an inert staging composer must not consume a one-shot composer request')
  assert.match(
    shell,
    /runtimeActive=\{surfaceVisible && chatPanesVisible && role !== 'held'\}[\s\S]*keepTranscriptPainted=\{surfaceVisible && role === 'held'\}/,
    'Shell must explicitly distinguish the inactive held runtime from its painted cover',
  )
  assert.match(
    paneChatView,
    /hidden=\{!runtimeActive\}[\s\S]*keepTranscriptPainted=\{keepTranscriptPainted\}/,
    'the pane boundary must pass both independent responsibilities to ChatView',
  )
  assert.match(
    chatView,
    /if \(!hidden\) return[\s\S]*if \(keepTranscriptPainted\) return[\s\S]*setInitialEntryPhase\('history'\)[\s\S]*setLoading\(true\)/,
    'a held cover must relinquish runtime ownership without arming the transcript blanking gate',
  )
})

test('only the painted workspace world can expose its handoff layers', () => {
  assert.match(
    shell,
    /const layoutPaneId = presentationPaneId \?\? paneId[\s\S]*const paneActiveKey = paneModel\.activeKeyForOwner\(workspace, layoutPaneId\) \|\| tabKey/,
    'handoff visibility must follow the owner current content, including the synthetic single-screen owner',
  )
  assert.match(
    shell,
    /const surfaceVisible = !!\(paned \|\| fullBleed\)/,
    'a retained owner in the hidden world must remain mounted without becoming visible',
  )
  assert.match(
    shell,
    /const handoffClass = !settingsOverlay && surfaceVisible && role !== 'active'/,
    'held and staging visibility classes belong only to the world that is actually painting',
  )
  assert.match(
    shell,
    /data-chat-surface=\{surfaceVisible && role === 'active' \? 'painted' : undefined\}/,
    'browser contracts need one explicit selector for the settled interactive chat surface',
  )
  assert.match(
    shell,
    /inert=\{!surfaceVisible \|\| settingsOverlay \|\| role !== 'active'/,
    'a retained chat in the parked workspace world must remain inert',
  )
  assert.match(
    shell,
    /aria-hidden=\{!surfaceVisible \|\| settingsOverlay \|\| role !== 'active'/,
    'a retained chat in the parked workspace world must leave the accessibility tree',
  )
})

test('app-supplied drafts update retained composers as well as remounted chats', () => {
  assert.match(shell,
    /navToRef\.current\('chat', \{ chatId: request\.chatId \}\)[\s\S]*requestComposer\(request\.chatId, \{ draft: draftText \}\)/,
    'the open-chat handoff must target the live composer after navigation')
  assert.match(chatView,
    /typeof composerRequest\.draft === 'string'[\s\S]*handleComposerInputChange\(composerRequest\.draft\)/,
    'a retained ChatView must apply the requested draft to controlled state')
  assert.match(chatView, /if \(!shouldApplyComposerFocusRequest\(\{[\s\S]*onComposerRequestHandled\?\.\(token\)/,
    'a draft-only handoff must settle without stealing focus')
})

test('direct chat actions hand focus to the destination composer', () => {
  assert.match(shell,
    /startupChatComposerFocusPendingRef = useRef\([\s\S]*activeView === 'chat' && effectiveViewMode === 'single'[\s\S]*\)/,
    'only a restored single-screen chat may retain the startup focus intent')
  assert.match(shell,
    /if \(!startupChatComposerFocusPendingRef\.current\) return[\s\S]*activeView !== 'chat' \|\| activeChatId == null[\s\S]*startupChatComposerFocusPendingRef\.current = false[\s\S]*focusDesktopChatPaneComposer\(activeChatId\)/,
    'restored chat focus must be one-shot rather than following later mode changes')
  const startUserChat = shell.match(
    /function startUserChat\(\) \{([\s\S]*?)\n  \}/,
  )?.[1] || ''
  assert.match(startUserChat,
    /const forceNew = workspaceStateRef\.current\.ws\.viewMode === 'panes'/,
    'only Builder makes the owner-facing New chat action additive')
  assert.match(startUserChat,
    /newChat\(\{ forceNew, focusComposer: true, recordHistory: true \}\)/,
    'the mode-scoped action must still focus the destination composer')
  assert.match(shell, /onClick=\{startUserChat\}/,
    'the desktop rail must use the shared mode-scoped action')
  assert.match(shell, /onNewChat=\{startUserChat\}/,
    'the mobile drawer must use the shared mode-scoped action')
  assert.match(shell,
    /beginTouchComposerFocusLease\([\s\S]*?await resolveNewChatId/,
    'New chat must reserve phone keyboard focus before its first async boundary')
  assert.match(shell,
    /composerFocusLeaseRef\.current\?\.value[\s\S]*?composerFocusLeaseHandoff\(\{[\s\S]*?stageComposerHandoff\(chatId, handoff\.text/,
    'New chat must carry early lease typing into the destination composer')
  assert.match(shell,
    /String\(presentation\?\.chatId \?\? ''\) === id[\s\S]*?requestComposer\(id, \{ focus: true \}\)/,
    'an immediate New chat presentation must hand focus over at display readiness')
  assert.match(shell,
    /if \(focusComposer && \(!presentation \|\| alreadyPresented\)\) \{[\s\S]*?requestComposer\(chatId/,
    'allocation must not consume the focus request before the presentation is ready')
  assert.match(shell,
    /className="shell__composer-focus-lease"[\s\S]*?aria-label="New chat message"/,
    'the keyboard lease must remain a named, programmatically focused text control')
  const selectChat = shell.match(
    /function selectChat\(id, \{ focusComposer = true \} = \{\}\) \{([\s\S]*?)\n  \}/,
  )?.[1] || ''
  assert.match(selectChat,
    /navTo\('chat', \{ chatId: id, preserveDrawerPresentation \}\)[\s\S]*if \(focusComposer\) focusDesktopChatPaneComposer\(id\)/,
    'drawer and settings chat selection must focus after requesting navigation')
  assert.match(shell,
    /target\.focusComposer === true[\s\S]*requestComposer\(target\.chatId, \{ focus: true \}\)/,
    'the shared header navigation boundary preserves title-only search focus')
  assert.match(chatView,
    /className=\{`chat__msg[\s\S]*tabIndex=\{-1\}/,
    'message rows must accept the programmatic search focus without joining tab order')
  assert.match(shell,
    /onActivate=\{\(\) => \{[\s\S]*tabModel\.tabNavTarget\(tab\)[\s\S]*navTo\(view, opts\)[\s\S]*tab\.kind === 'chat'[\s\S]*focusDesktopChatPaneComposer\(tab\.id\)/,
    'the single-pane tab strip must focus a selected chat composer')
  const selectedChatHandoffs = workspaceChrome.match(
    /if \(tab\.kind === 'chat'\) onChatPaneSelected\?\.\(tab\.id\)/g,
  ) || []
  assert.equal(selectedChatHandoffs.length, 2,
    'both active-tab and tab-switch paths in a tiled pane must focus chat composers')
})

test('the held chat is an opaque layer above staging until the atomic swap', () => {
  assert.match(ruleBody('.shell__chat-view'), /background:\s*var\(--bg\)/,
    'the cover must be opaque so hidden incoming content cannot leak through')
  assert.match(ruleBody('.shell__chat-view'), /isolation:\s*isolate/,
    'chat-owned z-index layers must not escape above shell-owned presentations')
  assert.match(ruleBody('.shell__chat-view--staging'), /visibility:\s*visible/)
  assert.match(ruleBody('.shell__chat-view--staging'), /z-index:\s*1/)
  assert.match(ruleBody('.shell__chat-view--held'), /visibility:\s*visible/)
  assert.match(ruleBody('.shell__chat-view--held'), /z-index:\s*2/,
    'the last painted frame must stay above the staging mount')
})

test('chat selection swaps atomically without flashing or animating text layers', () => {
  const drawerItem = ruleBody('.drawer__item', drawerCss)
  const drawerPress = ruleBody('.drawer__item:not(.drawer__item--active):active', drawerCss)
  assert.equal(
    drawerItem.match(/transition:\s*([^;]+);/)?.[1],
    'background-color 0.12s',
    'the selected title color must snap instead of tweening its glyphs')
  assert.doesNotMatch(indexCss, /\.drawer__item:active\b/,
    'drawer press feedback must stay out of the global scale contract')
  assert.match(drawerPress, /background-color:\s*var\(--surface\)/,
    'an unselected row should retain quiet press feedback')
  assert.doesNotMatch(drawerPress, /transform:/,
    'drawer press feedback must keep the row geometry fixed')

  assert.doesNotMatch(shellCss, /\.shell__chat-view(?:--staging)? > \.chat/,
    'the ready transcript must replace the opaque cover in one fully painted frame')
  assert.doesNotMatch(ruleBody('.shell__chat-view--held'), /opacity:/,
    'the outgoing transcript must remain opaque until the atomic swap')
  assert.match(drawerCss,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.drawer__item \{\s*transition:\s*none;/,
    'the drawer background wash should disappear under reduced motion')
})
