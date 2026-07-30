import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const css = readFileSync(
  new URL('../workspace.css', import.meta.url),
  'utf8',
)
const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
const shellBrand = readFileSync(new URL('../ShellBrand.jsx', import.meta.url), 'utf8')
const newChatLanding = readFileSync(new URL('../NewChatLanding.jsx', import.meta.url), 'utf8')
const workspaceViewSrc = readFileSync(new URL('../workspaceView.js', import.meta.url), 'utf8')
const drawer = readFileSync(new URL('../../Drawer/Drawer.jsx', import.meta.url), 'utf8')
const drawerItemActionMenu = readFileSync(
  new URL('../../Drawer/DrawerItemActionMenu.jsx', import.meta.url),
  'utf8',
)
const paneModelSrc = readFileSync(new URL('../paneModel.js', import.meta.url), 'utf8')
const chrome = readFileSync(new URL('../WorkspaceChrome.jsx', import.meta.url), 'utf8')
const dragBinding = readFileSync(new URL('../useWorkspaceDrag.js', import.meta.url), 'utf8')
const paneStrip = readFileSync(new URL('../PaneStrip.jsx', import.meta.url), 'utf8')
const settingsView = readFileSync(
  new URL('../../SettingsView/SettingsView.jsx', import.meta.url), 'utf8',
)
const walkthrough = readFileSync(
  new URL('../../Walkthrough/WalkthroughOverlay.jsx', import.meta.url), 'utf8',
)
const walkthroughCss = readFileSync(
  new URL('../../Walkthrough/WalkthroughOverlay.css', import.meta.url), 'utf8',
)

test('the workspace menu avoids an oversized border-and-shadow card', () => {
  const rule = css.match(/\.workspace__menu\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /border:\s*1px/)
  assert.match(rule, /box-shadow:\s*0 4px 8px/)
  assert.doesNotMatch(rule, /box-shadow:[^;]*(?:1[6-9]|[2-9]\d)px/)
})

test('the workspace menu stays close-only, edge-clamped, and keyboard navigable', () => {
  const menuMarkup = shell.slice(
    shell.indexOf('{tabMenu &&'),
    shell.indexOf('</HistoryDismissProvider>'),
  )
  assert.match(shell, /aria-label="Tab actions"/)
  assert.match(menuMarkup, /Close tab/)
  assert.match(menuMarkup, /Close all other tabs/)
  assert.match(menuMarkup, /Close tabs to the right/)
  assert.doesNotMatch(
    menuMarkup,
    /type: 'MOVE_TAB'|type: 'CLOSE_PANE'|Split |Move to |Close pane/,
  )
  assert.match(shell, /placeContextMenu\(\{/)
  assert.match(shell, /event\.key === 'ArrowDown'/)
  assert.match(shell, /querySelector\('\[role="menuitem"\]'\)\?\.focus\(\)/)
  assert.match(shell, /tabMenuReturnFocusRef\.current = event\.currentTarget/)
  assert.match(shell, /returnTarget\?\.focus\?\.\(\{ preventScroll: true \}\)/)
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.workspace__menu-header/)
})

test('an implicit home tab does not engage the single-pane tab strip', () => {
  // Only a fallback workspace may be treated as implicit. A valid one-leaf
  // single-screen blob intentionally has an empty legacy mirror; resetting it
  // on a deep link would silently change its view mode back to builder.
  assert.match(shell, /const replaceImplicitBootTab = !blobValid\s*\n?\s*&& legacyOpenTabs\.length === 0/)
  assert.match(shell, /const \[tabStripEngaged, setTabStripEngaged\] = useState\(legacyOpenTabs\.length > 0\)/)
  assert.match(shell, /if \(openTabs\.length >= 2\) setTabStripEngaged\(true\)/)
  assert.match(shell, /else if \(openTabs\.length === 0\) setTabStripEngaged\(false\)/)
  // With splits ON the strip follows the EFFECTIVE builder world only (never
  // single mode or an immersive takeover); the engaged latch is the kill-switch
  // world's legacy rule.
  assert.match(shell, /const tabStripVisible = !immersiveActive\s*\n?\s*&& \(SPLITS \? effectiveViewMode === 'panes' : tabStripEngaged\)\s*\n?\s*&& openTabs\.length >= 1/)
  assert.match(shell, /tabStripEngaged[\s\S]*?paneModel\.flattenRollbackPriority\(workspace\)[\s\S]*?: \[\]/)
  // v2 DELETED the legacy sole-tab "unpin" shortcut (deletion list): the sole-tab
  // close is always a real CLOSE_TAB now, so an emptied builder auto-returns to
  // single. The ONE unified close takes a tab object + opts (INV 13).
  assert.doesNotMatch(shell, /openTabs\.length === 1 && kind !== 'settings'/)
  assert.match(shell, /const closeTab = useCallback\(\(tab, \{ reason \} = \{\}\)/)
})

test('the drop preview reads as an 18% accent fill with a 2px border and morph', () => {
  const rule = css.match(/\.workspace__drop-preview\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /border:\s*2px solid var\(--accent\)/)
  assert.match(rule, /var\(--accent\)\s*18%/)
  assert.match(rule, /border-radius:\s*10px/)
  // First-appear fade (60ms) + zone-to-zone morph (90ms cubic-bezier) — the faster
  // morph makes the larger uncapped bands feel even more responsive.
  assert.match(rule, /opacity 60ms/)
  assert.match(rule, /90ms cubic-bezier\(0\.2, 0, 0, 1\)/)
})

test('the strip caret variant drops the fill and border for a solid bar', () => {
  const rule = css.match(/\.workspace__drop-preview--caret\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /border:\s*none/)
  assert.match(rule, /background:\s*var\(--accent\)/)
})

test('the drag chip is a pointer-transparent fixed layer with a [hidden] guard', () => {
  const rule = css.match(/\.workspace__drag-chip\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /position:\s*fixed/)
  assert.match(rule, /pointer-events:\s*none/)
  assert.match(css, /\.workspace__drag-chip\[hidden\]\s*\{\s*display:\s*none/)
})

test('the drag layer covers the viewport visually but can never block navigation', () => {
  const rule = css.match(/\.workspace__drag-shield\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /position:\s*fixed/)
  assert.match(rule, /inset:\s*0/)
  assert.match(rule, /pointer-events:\s*none/)
  assert.match(rule, /cursor:\s*grabbing/)
  // The visual layer may out-layer the drawer, but pointer capture — not this
  // transparent DOM node — owns a live drag. An orphaned layer therefore cannot
  // leave a visible drawer untappable.
  const z = Number(rule.match(/z-index:\s*(\d+)/)?.[1] || 0)
  assert.ok(z >= 100, `drag layer z-index ${z} must paint above the drawer (95)`)
})

test('reduced motion makes the drop preview instant', () => {
  const block = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(block, /\.workspace__drop-preview\s*\{\s*transition:\s*none/)
})

test('the retired first-use drag hint cannot reappear in the shell', () => {
  assert.doesNotMatch(shell, /workspaceCoachmarkVisible|Drag a tab to move or split it/)
  assert.doesNotMatch(css, /\.workspace__coachmark/)
})

test('post-drag click suppression is source-scoped and expires on fresh input', () => {
  assert.match(dragBinding, /function suppressNextSourceClick\(sourceEl\)/)
  assert.match(dragBinding, /path\.includes\(sourceEl\)/)
  assert.match(dragBinding, /if \(!belongsToSource\) return/)
  assert.match(dragBinding, /window\.addEventListener\('pointerdown', clear, true\)/)
  assert.match(dragBinding, /window\.removeEventListener\('pointerdown', clear, true\)/)
  assert.match(dragBinding, /suppressNextSourceClick\(srcEl\)/)
})

test('the undo chord is flag-gated and defers to focused inputs', () => {
  assert.match(shell, /if \(!paneModel\.WORKSPACE_SPLITS_ENABLED\) return undefined[\s\S]*?undoKeyPressed\(e\)/)
  assert.match(shell, /isEditableTarget\(document\.activeElement\)/)
  assert.match(shell, /dispatchWorkspace\(\{ type: 'UNDO_LAST' \}\)/)
})

test('the first-run walkthrough stays short and action-first', () => {
  assert.doesNotMatch(walkthrough, /const STEPS/)
  assert.match(walkthrough, /Your Möbius is ready/)
  assert.match(walkthrough, /Connect an agent/)
  assert.match(walkthrough, /Open the App Store/)
  assert.match(walkthrough, /Keep Möbius close/)
  assert.match(walkthrough, /requestInstall/)
  assert.match(walkthrough, /I’ll explore/)
  assert.match(walkthrough, /mobius:walkthrough-completed/)
})

test('the first-run walkthrough remains dismissible in a short landscape viewport wider than 520px', () => {
  const shortLandscape = { width: 700, height: 360 }
  assert.ok(shortLandscape.width > 520)
  assert.ok(shortLandscape.height < 520)

  const baseCardRule = walkthroughCss.match(/\.wt__card\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(
    baseCardRule,
    /max-height:\s*calc\(100dvh - 80px - env\(safe-area-inset-top,\s*0px\)\)/,
    'the viewport-height cap must apply outside the phone-width media query',
  )
  assert.match(baseCardRule, /overflow-y:\s*auto/,
    'clipped actions must remain reachable by scrolling')
  assert.match(baseCardRule, /overscroll-behavior:\s*contain/,
    'scrolling the coach card must not move the workspace behind it')
  assert.doesNotMatch(baseCardRule, /overflow:\s*hidden/,
    'the width-independent card rule must never clip its final actions')
})

test('the authenticated shell offers a keyboard skip link', () => {
  assert.match(shell, /href="#main-content"/)
  assert.match(shell, /event\.preventDefault\(\)[\s\S]*?contentElRef\.current\?\.focus\(\{ preventScroll: true \}\)/)
  assert.match(shell, /<main className="shell__content" id="main-content" tabIndex=\{-1\}/)
})

test('drawer lists distinguish loading, error, and confirmed empty data', () => {
  assert.match(shell, /appsStatus=\{appsStatus\}/)
  assert.match(shell, /chatsStatus=\{chatsStatus\}/)
  assert.match(drawer, /chatsStatus === 'loading' \|\| appsStatus === 'loading'/)
  assert.match(drawer, /chatsStatus === 'error' \|\| appsStatus === 'error'/)
  assert.match(drawer, /Loading recents…/)
  assert.match(drawer, /Recents unavailable\./)
  assert.match(drawer, /Nothing recent yet/)
})

test('a crashed app pane is isolated by a per-pane ErrorBoundary', () => {
  // The AppCanvas wrapper is wrapped in its own inline ErrorBoundary so one
  // canvas throw degrades locally instead of replacing the whole shell.
  assert.match(shell, /<ErrorBoundary key=\{`ab-\$\{id\}`\} variant="inline" label="app">/)
})

test('the divider drag tears down from the window, surviving a mid-drag unmount', () => {
  // Window-bound listeners + a lostpointercapture teardown restore body
  // user-select even if the divider handle unmounts mid-drag.
  assert.match(chrome, /window\.addEventListener\('lostpointercapture', end\)/)
  assert.match(chrome, /document\.body\.style\.userSelect = prevUserSelect/)
})

test('the context menu offers pane-scoped bulk close actions only when useful', () => {
  assert.match(shell, /const hasSiblingTabs = Boolean\(menuPane && menuPane\.tabs\.length > 1\)/)
  assert.match(shell, /type: 'CLOSE_OTHER_TABS',[\s\S]*?tabKey: tabMenu\.tabKey/)
  assert.match(shell, /Close all other tabs/)
  assert.match(shell, /const hasTabsToRight = Boolean\(/)
  assert.match(shell, /type: 'CLOSE_TABS_TO_RIGHT',[\s\S]*?tabKey: tabMenu\.tabKey/)
  assert.match(shell, /Close tabs to the right/)
})

test('tab labels resolve through memoized id Maps, not per-render linear scans', () => {
  // labelForTab and the single-pane strip use O(1) Map lookups keyed by id.
  assert.match(shell, /const chatById = useMemo/)
  assert.match(shell, /const appById = useMemo/)
  assert.match(shell, /chatById\.get\(tab\.id\)/)
  assert.doesNotMatch(shell, /chats\.find\(c => String\(c\.id\) === tab\.id\)/)
})

test('the divider and drag paths coalesce their per-move work into a rAF', () => {
  assert.match(chrome, /rafId = requestAnimationFrame\(\(\) => \{ rafId = 0; paint/)
  assert.match(dragBinding, /moveRAF = requestAnimationFrame\(doMoveWork\)/)
})

test('paned wrappers carry NO layout-property transition and NO resize guard (v2)', () => {
  // v2 (exit-presentation): the 180ms geometry bloom, BOTH guard classes
  // (workspace--container-resizing / workspace--divider-dragging), and the 200ms
  // ResizeObserver timer are DELETED. A mode beat animates transform only, and a
  // divider drag writes rects imperatively — there is no layout interpolation to
  // suppress, so discrete commits simply snap.
  const rule = css.match(/\.shell__view--paned\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.doesNotMatch(rule, /transition:/)
  assert.doesNotMatch(css, /workspace--container-resizing/)
  assert.doesNotMatch(css, /workspace--divider-dragging/)
  assert.doesNotMatch(shell, /workspace--container-resizing/)
  assert.doesNotMatch(chrome, /workspace--divider-dragging/)
})

test('strips sit above dividers so the 44px grab never occludes a tab', () => {
  const rule = css.match(/\.workspace__strip\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /z-index:\s*5/)
})

test('divider hover feedback stays compositor-only', () => {
  const rule = css.match(/\.workspace__divider-bar\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /transition:\s*background 120ms ease, transform 120ms ease/)
  assert.doesNotMatch(rule, /transition:[^;]*(?:width|height)/)
  assert.match(css, /workspace__divider--v:focus-visible \.workspace__divider-bar \{ transform: scaleX\(3\); \}/)
  assert.match(css, /workspace__divider--h:focus-visible \.workspace__divider-bar \{ transform: scaleY\(3\); \}/)
})

test('pane strips use a complete horizontal tab keyboard and ownership contract', () => {
  assert.match(paneStrip, /export function stripKeyDown/)
  assert.match(paneStrip, /tabIndex=\{active \? 0 : -1\}/)
  assert.match(paneStrip, /\(i \+ 1\) % buttons\.length/)
  assert.match(paneStrip, /\(i - 1 \+ buttons\.length\) % buttons\.length/)
  assert.doesNotMatch(paneStrip, /e\.key === 'ArrowDown'/)
  assert.doesNotMatch(paneStrip, /e\.key === 'ArrowUp'/)
  assert.match(paneStrip, /if \(neighbour\) neighbour\.focus\(\)/)
  assert.match(paneStrip, /document\.querySelector\('\.shell__brand'\)\?\.focus\(\)/)
  assert.match(paneStrip, /aria-controls=\{role === 'tab' \? controlsId : undefined\}/)
  assert.match(paneStrip, /export function paneTabDomId/)
  assert.match(paneStrip, /export function panePanelDomId/)
  assert.match(shell, /role=\{paned \? 'tabpanel' : undefined\}/)
  assert.match(shell, /aria-labelledby=\{paned \? paneTabDomId\(paned\.paneId, tabKey\) : undefined\}/)
  // Both containers route their keydown through the shared roving helper.
  assert.match(paneStrip, /stripKeyDown\(e, pane\.tabs, onClose\)/)
  assert.match(shell, /stripKeyDown\(e, openTabs,/)
})

test('middle-click closes a tab through the shared close path (web/desktop only)', () => {
  // auxclick with button 1 routes to the SAME onClose the ✕ uses — no parallel
  // close mechanism (identical undo/history semantics). Shared by both strips
  // because PaneTab is the one tab implementation.
  assert.match(paneStrip, /onAuxClick=\{\(e\) => \{ if \(e\.button === 1\) \{ e\.preventDefault\(\); onClose\(\) \} \}\}/)
  // mousedown button 1 is prevented so the platform autoscroll circle never shows.
  assert.match(paneStrip, /onMouseDown=\{\(e\) => \{ if \(e\.button === 1\) e\.preventDefault\(\) \}\}/)
  // A middle press can never arm a drag: the drag hook bails on any non-primary
  // mouse button before it reads data-drag-key.
  assert.match(dragBinding, /if \(e\.pointerType === 'mouse' && e\.button !== 0\) return/)
})

test('the single-pane strip derives active from the workspace, retiring isTabActive', () => {
  assert.match(shell, /active = key === focusedActiveKey/)
  // No live CALL to the retired legacy-triple predicate.
  assert.doesNotMatch(shell, /tabModel\.isTabActive\(/)
  assert.doesNotMatch(paneStrip, /isTabActive\(/)
})

test('builder mode has no extra top-right pane affordance', () => {
  assert.doesNotMatch(chrome, /Layers|Show panes|workspace__pane-chip|workspace__sheet/)
  assert.doesNotMatch(css, /\.workspace__pane-chip|\.workspace__sheet/)
})

test('workspace mutations update the undo slot silently, with no toast', () => {
  // The reducer still mints an undo slot every mutation (its own tests lock
  // that), but the shell no longer surfaces a "Moved X · Undo" / agent-placement
  // toast — the owner found them noisy. Recovery is the Cmd/Ctrl+Z chord.
  assert.doesNotMatch(shell, /wsUndo:\s*true/)
  assert.doesNotMatch(shell, /message:\s*slot\.toast/)
  // The chord itself must remain.
  assert.match(shell, /dispatchWorkspace\(\{ type: 'UNDO_LAST' \}\)/)
})

test('the focused pane carries no always-on ring, only an active-tab signal', () => {
  // No persistent ring element or its stylesheet rule.
  assert.doesNotMatch(chrome, /data-focus-ring/)
  assert.doesNotMatch(chrome, /workspace__focus-ring/)
  assert.doesNotMatch(css, /\.workspace__focus-ring\s*\{/)
  // Which tab is open per pane, and which pane has focus, read from the active
  // pill: the focused strip's active pill gets a 2px accent underline; unfocused
  // strips' active pills soften instead.
  assert.match(css, /\.workspace__strip--focused \.shell__tab--active\s*\{[\s\S]*?inset 0 -2px 0/)
  assert.match(css, /\.workspace__strip:not\(\.workspace__strip--focused\) \.shell__tab--active/)
})

test('keyboard pane focus is visible but stays off for mouse and touch', () => {
  // A keyboard-only outline on the pane's strip — never an always-on ring.
  assert.match(css, /\.workspace__strip:has\(\.shell__tab-open:focus-visible\)\s*\{[\s\S]*?outline:/)
})

// ── Builder-mode control + logo shortcuts ──────────────────────────────────

const logoGestureSrc = readFileSync(new URL('../useLogoModeGesture.js', import.meta.url), 'utf8')
const shellCss = readFileSync(new URL('../Shell.css', import.meta.url), 'utf8')
const drawerCss = readFileSync(new URL('../../Drawer/Drawer.css', import.meta.url), 'utf8')

test('the docked sidebar offsets only direct shell layout rows', () => {
  // Pane strips reuse .shell__tabstrip inside .shell__content. A descendant
  // selector would apply the 320px sidebar margin twice and detach every strip
  // from the pane rectangle that owns it.
  assert.match(shellCss, /\.shell--drawer-docked > \.shell__tabstrip,/)
  assert.match(shellCss, /\.shell--drawer-docked > \.shell__content/)
  assert.match(shellCss, /\.shell--immersive\.shell--drawer-docked > \.shell__tabstrip,/)
  assert.match(shellCss, /\.shell--immersive\.shell--drawer-docked > \.shell__content/)
  assert.doesNotMatch(shellCss, /\.shell--drawer-docked \.shell__tabstrip/)
})

test('the header never grows a standalone pane-mode icon', () => {
  assert.match(shell, /<header className="shell__bar"/)
  // Owner contract: hold/swipe the top-left Möbius brand or drag from the drawer.
  // A second top-right affordance is redundant and must not quietly return.
  assert.doesNotMatch(shell, /PanelsTopLeft|shell__mode-toggle|Use panes|Use single screen/)
  assert.doesNotMatch(shell, /ViewModeToggle|shell__viewmode/)
  assert.doesNotMatch(shellCss, /\.shell__(?:mode-toggle|viewmode)\b/)
})

test('the SINGLE tap keeps its drawer job — instant, NO setTimeout on the tap path', () => {
  // The brand button is the drawer trigger; onClick toggles it synchronously after
  // a suppressed-gesture check, with zero timers.
  assert.match(shellBrand, /className=\{`shell__brand/)
  assert.match(shellBrand, /aria-expanded=\{navigationOpen\}/)
  const onClick = shellBrand.match(/onClick=\{\(e\) => \{[\s\S]*?\n {8}\}\}/)?.[0] || ''
  assert.match(onClick, /if \(logoGesture\.consumeSuppressedClick\(e\.detail\)\) return/)
  assert.match(onClick, /onToggleNavigation\(\)/)
  assert.doesNotMatch(onClick, /setTimeout\(/, 'the tap path must carry no timer')
})

test('HOLD (~450ms) and touch swipe-right flip the mode; the hook never touches the drawer', () => {
  // Thresholds + predicates are the pure machine; the hook composes them.
  const machineSrc = readFileSync(new URL('../logoHoldMachine.js', import.meta.url), 'utf8')
  assert.match(machineSrc, /export const HOLD_MS = 450/)
  assert.match(machineSrc, /export const SWIPE_DX = 28/)
  // The hook drives completion off the rAF loop (no setTimeout), fires the mode
  // flip, and marks the click suppressed so the gesture never also opens the drawer.
  assert.match(logoGestureSrc, /p >= 1\) \{ completeHold\(\); return \}/)
  assert.doesNotMatch(logoGestureSrc, /setTimeout\(/, 'no timer — the rAF loop owns the hold')
  // pointerType gates the swipe (finding F12): mouse drags classify as cancel.
  assert.match(logoGestureSrc, /decidePointerMove\(dx, dy, press\.pointerType\)/)
  assert.match(logoGestureSrc, /decision === 'swipe'/)
  // The gesture threads the HONEST cause (finding F13): 'hold' on a completed hold,
  // 'swipe' on a swipe — never a bare onToggleMode?.() that the controller mislabels.
  assert.match(logoGestureSrc, /onToggleMode\?\.\('hold'\)/)
  assert.match(logoGestureSrc, /onToggleMode\?\.\('swipe'\)/)
  assert.match(logoGestureSrc, /endPress\(\{ suppressClick: true \}\)/)
  // Suppresses the native long-press context menu for a FRESH touch/pen (or any
  // live press) so a hold activates builder mode instead of raising a menu.
  assert.match(logoGestureSrc, /\(\(pt === 'touch' \|\| pt === 'pen'\) && fresh\) \|\| pressRef\.current\) e\.preventDefault\(\)/)
  // The hook itself never opens/closes the drawer — that stays the caller's.
  assert.doesNotMatch(logoGestureSrc, /openDrawer|closeDrawer/)
})

test('the press state machine is pointer-captured, keyed, and classified by time+displacement', () => {
  // §5: pointerId stored + pointer capture taken; move/up/cancel ignore other pointers.
  assert.match(logoGestureSrc, /pointerId: e\.pointerId/)
  assert.match(logoGestureSrc, /setPointerCapture\?\.\(e\.pointerId\)/)
  assert.match(logoGestureSrc, /releasePointerCapture\?\.\(press\.pointerId\)/)
  assert.match(logoGestureSrc, /e\.pointerId !== press\.pointerId\) return/)
  assert.match(logoGestureSrc, /if \(pressRef\.current\) return \/\/ a press is already live/)
  // §4: pointerup classifies by elapsed + displacement, not liveness.
  assert.match(logoGestureSrc, /if \(swipeAllowed\(press\.pointerType\) && isSwipeRight\(dx, dy\)\) \{ onToggleMode\?\.\('swipe'\); endPress\(\{ suppressClick: true \}\); return \}/)
  // Displacement rules out the mode FLIP only. Suppressing the click here as well
  // duplicated the browser's own tap-vs-drag decision with a stricter 10px slop, so
  // an ordinary drifting thumb tap on the brand silently failed to open the drawer.
  // Abandoned presses (movement, cancel, lost capture) must leave the click alone.
  assert.match(logoGestureSrc, /if \(movedBeyondSlop\(dx, dy\)\) \{ endPress\(\{ suppressClick: false \}\); return \}/)
  assert.equal((logoGestureSrc.match(/endPress\(\{ suppressClick: false \}\)/g) || []).length, 5,
    'the abandon paths (move-cancel, up-with-drift, pointercancel, lost capture) plus '
    + 'the plain tap all leave the click to the browser')
  assert.match(logoGestureSrc, /if \(holdComplete\(elapsed\)\) \{ completeHold\(\); return \}/)
  // §6: a drawer-open from any path cancels a live hold.
  assert.match(logoGestureSrc, /if \(drawerOpen && pressRef\.current\) endPress/)
  // §13: a keyboard click (detail 0) is never suppressed.
  assert.match(logoGestureSrc, /if \(detail === 0\) return false/)
})

test('completion feedback (SINGLE PULSE): one completion haptic, NO mid-hold ramp ticks', () => {
  // navigator.vibrate is feature-detected (iOS has none → graceful no-op).
  assert.match(logoGestureSrc, /typeof navigator\.vibrate === 'function'/)
  assert.match(logoGestureSrc, /runHoldCompletion\(\{/)
  // Direction is read from the CURRENT mode: entering builder springs, exiting snaps.
  assert.match(logoGestureSrc, /const entering = !builderModeActive/)
  // Owner call 2026-07-19: the mid-hold ramp ticks (50% + 85%) are GONE — three
  // pulses in a ~450ms hold read as a buzzy double/triple tap ("feels like two
  // vibrations instead of one"). No ramp state, no ramp constants anywhere, and
  // the rAF tick loop fires no haptic — the single completion pulse is the ONLY
  // vibration.
  const machineSrc = readFileSync(new URL('../logoHoldMachine.js', import.meta.url), 'utf8')
  assert.doesNotMatch(logoGestureSrc, /rampRef|ramp\.t1|ramp\.t2|RAMP_TICK/)
  assert.doesNotMatch(machineSrc, /RAMP_TICK/)
  const tickBody = logoGestureSrc.match(/const tick = useCallback\(\(\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  assert.doesNotMatch(tickBody, /vibrate/, 'the hold tick loop fires no haptic — only completeHold does')
  // The spring/snap one-shot is restarted (clear-then-set) and cleared on animationend.
  assert.match(logoGestureSrc, /setFlourish\(''\)\s*\n\s*requestAnimationFrame\(\(\) => setFlourish\(isEntering \? 'igniting' : 'snapping'\)\)/)
  assert.match(logoGestureSrc, /const onAnimationEnd = useCallback\(\(\) => \{ setFlourish\(''\) \}, \[\]\)/)
  // The rAF is cancelled on unmount so a hold in flight can't tick a dead component.
  assert.match(logoGestureSrc, /useEffect\(\(\) => \(\) => \{ stopRaf\(\) \}, \[stopRaf\]\)/)
})

test('ShellBrand isolates gesture state and wires the brand ref + Shift+Enter', () => {
  const handler = shell.match(/const handleToggleViewMode = useCallback\(\(cause\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  // v2: the toggle builds the latched plan + flips the durable mode in one handler;
  // there is no Settings conversion call anymore (the tab survives the flip).
  assert.doesNotMatch(handler, /convertSettingsForModeTransition/)
  assert.match(handler, /mode\.toggle\(\{ cause, presentation \}\)/)
  assert.match(handler, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE', mode: 'toggle' \}\)/)
  assert.doesNotMatch(handler, /openDrawer|closeDrawer/)
  // The gesture hook receives the toggle + the brand ref (for the ring var). The
  // ref is UNIFIED with the desktop-sidebar focus ref (one ref, both jobs) after
  // the sidebar rebase.
  assert.doesNotMatch(shell, /useLogoModeGesture\(/)
  assert.match(shellBrand, /const ShellBrand = memo\(function ShellBrand/)
  assert.match(shellBrand, /useLogoModeGesture\(\{[\s\S]*?onToggleMode,/)
  assert.match(shell, /<ShellBrand[\s\S]*?brandRef=\{brandButtonRef\}/)
  // The drag-deny vibrate is DEAD (point 15: dragging is building, never denied).
  assert.doesNotMatch(shell, /viewModeVibrateRef|onDragBlocked/)
  // Keyboard path: Shift+Enter flips the mode (preventDefault keeps it off the drawer).
  assert.match(shellBrand, /e\.shiftKey && e\.key === 'Enter'/)
  assert.match(shellBrand, /keyboardModeClickRef\.current = true/)
  assert.match(shellBrand, /keyboardModeClickRef\.current && e\.detail === 0/)
})

test('the logo mark IS the indicator (CHARGE): compress on hold + spring/snap + 180° twist + tint + static shared halo', () => {
  const brand = shellCss.match(/\.shell__brand\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(brand, /touch-action:\s*pan-y pinch-zoom/)
  assert.match(brand, /-webkit-touch-callout:\s*none/)
  // The conic hold RING is gone — the mark itself is the hold indicator.
  assert.doesNotMatch(shellCss, /\.shell__logo-ring/)
  assert.doesNotMatch(shellCss, /conic-gradient/)
  // Hold COMPRESS: base scale tracks --hold-progress; twist rides an independent
  // rotate property (so compress and twist compose, never clobber).
  const logoRule = shellCss.match(/\.shell__logo\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(logoRule, /scale:\s*calc\(1 - var\(--hold-progress, 0\) \* 0\.16\)/)
  assert.match(logoRule, /rotate:\s*var\(--logo-twist, 0deg\)/)
  assert.match(logoRule, /transition:\s*rotate 300ms cubic-bezier/)
  // The 180° twist is a var flip in builder mode (not a transform override).
  assert.match(shellCss, /\.shell__brand--builder \.shell__logo\s*\{[\s\S]*?--logo-twist:\s*180deg/)
  assert.match(shellCss, /\.shell__brand--builder \.shell__wordmark\s*\{[\s\S]*?color:\s*var\(--accent\)/)
  // INSTANT flip (empty tree) completion keeps the immediate ignite/snap (0.84→1);
  // an ANIMATED beat emits is-beat-held instead (round 4 item 1). Polish item 5's
  // same-beat timing survives (280ms ignite, not 480ms).
  assert.match(shellCss, /\.shell__brand\.is-igniting \.shell__logo\s*\{[\s\S]*?animation:\s*shell-logo-ignite 280ms cubic-bezier\(0\.16, 1, 0\.3, 1\)/)
  assert.match(shellCss, /\.shell__brand\.is-snapping \.shell__logo\s*\{[\s\S]*?animation:\s*shell-logo-snap 140ms cubic-bezier\(0\.25, 0\.8, 0\.25, 1\)/)
  assert.match(shellCss, /@keyframes shell-logo-ignite\s*\{[\s\S]*?scale:\s*0\.84[\s\S]*?scale:\s*1/)
  assert.match(shellCss, /@keyframes shell-logo-snap\s*\{[\s\S]*?scale:\s*0\.84/)
  // Round 4 item 1: a HOLD-owned animated beat holds .84 and RELEASES over the
  // terminal --logo-release-ms after --logo-release-delay (both fill), so the mark's
  // first full-size frame lands at completion. Two identical keyframes alternate by
  // epoch parity (a|b) so a retoggle restarts the delay by swapping the name.
  assert.match(shellCss, /\.shell__brand\.is-beat-held-a \.shell__logo\s*\{[\s\S]*?animation:\s*[\s\S]*?shell-logo-beat-release-a[\s\S]*?var\(--logo-release-ms, 120ms\)[\s\S]*?var\(--logo-release-delay, 0ms\)[\s\S]*?both/)
  assert.match(shellCss, /\.shell__brand\.is-beat-held-b \.shell__logo\s*\{[\s\S]*?animation:\s*[\s\S]*?shell-logo-beat-release-b[\s\S]*?var\(--logo-release-ms, 120ms\)[\s\S]*?var\(--logo-release-delay, 0ms\)[\s\S]*?both/)
  assert.match(shellCss, /@keyframes shell-logo-beat-release-a\s*\{[\s\S]*?scale:\s*0\.84[\s\S]*?scale:\s*1/)
  assert.match(shellCss, /@keyframes shell-logo-beat-release-b\s*\{[\s\S]*?scale:\s*0\.84[\s\S]*?scale:\s*1/)
  // Item 5 + round 4 item 1: logo rotate rides --mode-total (the plan's own totalMs)
  // so the twist settles with the panes — for a world reveal, at the end of the
  // pane beat. The wordmark tint keeps pace.
  assert.match(shellCss, /\.shell--builder-entering \.shell__logo\s*\{[\s\S]*?rotate var\(--mode-total, 260ms\) cubic-bezier\(0\.2, 1, 0\.32, 1\)/)
  assert.match(shellCss, /\.shell--builder-exiting \.shell__logo\s*\{[\s\S]*?rotate var\(--mode-total, 220ms\) cubic-bezier\(0\.25, 0\.8, 0\.25, 1\)/)
  assert.match(shellCss, /\.shell--builder-entering \.shell__wordmark \{ transition-duration: 220ms; \}/)
  assert.match(shellCss, /\.shell--builder-exiting \.shell__wordmark \{ transition-duration: 140ms; \}/)
  // Builder mode gets a faint static halo shared by the logo and wordmark.
  // Both shadows live on the existing leaves and have no animation/transition.
  const builderLogo = shellCss.match(/\.shell__brand--builder \.shell__logo\s*\{[\s\S]*?\}/)?.[0] || ''
  const builderWordmark = shellCss.match(/\.shell__brand--builder \.shell__wordmark\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(builderLogo, /filter:\s*drop-shadow\(/)
  assert.match(builderLogo, /var\(--accent\) 26%/)
  assert.match(builderWordmark, /text-shadow:/)
  assert.match(builderWordmark, /var\(--accent\) 30%/)
  assert.doesNotMatch(`${builderLogo}\n${builderWordmark}`, /animation:|transition:/)
  assert.doesNotMatch(shellCss, /\.shell__brand::after/)
  assert.doesNotMatch(shellCss, /shell__logo-halo|halo-opacity|halo-alpha/)
  assert.doesNotMatch(shellBrand, /useLivingHalo|requestAnimationFrame|logo-halo/)
  // Reduced motion: twist + compression/release snap immediately and spring/snap
  // is skipped (haptic still fires in JS).
  assert.match(shellCss, /\.shell__logo \{ transition: none; \}/)
  // The ignite/snap AND the hold's descriptor-owned beat-release are all disabled
  // under reduced motion (round 4 item 1 — belt-and-braces; is-beat-held is not even
  // emitted since the toggle commits instantly).
  assert.match(shellCss, /\.shell__brand\.is-igniting \.shell__logo,\s*\n\s*\.shell__brand\.is-snapping \.shell__logo,\s*\n\s*\.shell__brand\.is-beat-held-a \.shell__logo,\s*\n\s*\.shell__brand\.is-beat-held-b \.shell__logo \{ animation: none; \}/)
})

test('the brand logo img is pointer-inert so a hold never raises the native image preview', () => {
  // Owner phone report: "sometimes holding the logo opens up the image" - the
  // native long-press image callout/preview. Structural fix: the decorative img
  // (alt="") is pointer-inert so the BUTTON owns every pointer event and the
  // browser never sees a long-pressable image.
  const logoRule = shellCss.match(/\.shell__logo\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(logoRule, /pointer-events:\s*none/)
  assert.match(logoRule, /-webkit-touch-callout:\s*none/)
  assert.match(logoRule, /user-select:\s*none/)
  assert.match(logoRule, /-webkit-user-select:\s*none/)
  // The element itself is not draggable (kills the drag-image path).
  assert.match(shellBrand, /<img\s+className="shell__logo"[\s\S]*?draggable=\{false\}[\s\S]*?\/>/)
  // The button suppresses the native contextmenu for a FRESH touch/pen press —
  // recent pointer provenance, not merely a live press — closing the timing race
  // that leaked the native image menu just after a completed hold, while letting
  // a keyboard-invoked contextmenu on the focused brand reach the native menu
  // (provenance expires; keydown clears it).
  // while a press is live — which closes the timing race that leaked the menu: the
  // browser's long-press contextmenu can fire just AFTER the ~450ms hold completes
  // and nulls pressRef, so a press-only guard let the native image menu through.
  assert.match(logoGestureSrc, /const pt = lastPointerTypeRef\.current/)
  assert.match(logoGestureSrc, /\(\(pt === 'touch' \|\| pt === 'pen'\) && fresh\) \|\| pressRef\.current\) e\.preventDefault\(\)/)
})

test('logo pointer provenance EXPIRES so a keyboard context menu reaches the native menu (finding 5)', () => {
  // The touch/pen provenance justifies suppression only within a short window of the
  // pointerdown that stamped it (POINTER_PROVENANCE_MS) — otherwise a keyboard
  // contextmenu (Menu key / Shift+F10) on the focused brand, which has no pointer
  // event, inherits a stale 'touch'/'pen' and is wrongly suppressed (a11y regression).
  assert.match(logoGestureSrc, /const POINTER_PROVENANCE_MS = \d+/)
  assert.match(logoGestureSrc, /lastPointerTypeAtRef\.current = performance\.now\(\)/)
  assert.match(logoGestureSrc, /const fresh = \(performance\.now\(\) - lastPointerTypeAtRef\.current\) < POINTER_PROVENANCE_MS/)
  // A keydown on the brand also clears provenance so the next contextmenu is treated
  // as keyboard-invoked; Shell wires it into the brand's onKeyDown.
  assert.match(logoGestureSrc, /const onKeyDown = useCallback\(\(\) => \{\s*\n?\s*lastPointerTypeRef\.current = ''\s*\n?\s*lastPointerTypeAtRef\.current = 0/)
  assert.match(logoGestureSrc, /onKeyDown, onLostPointerCapture,\s*\n?\s*consumeSuppressedClick/)
  assert.match(shellBrand, /if \(splitsEnabled\) logoGesture\.onKeyDown\(\)/)
})

test('the Builder brand indicator is static and has no perpetual frame loop', () => {
  assert.doesNotMatch(shellBrand, /useLivingHalo|haloRef|logo-halo|requestAnimationFrame/)
  assert.doesNotMatch(shell, /haloActive=/)
  assert.doesNotMatch(shellCss, /shell__logo-halo|@keyframes [^{]*halo/)
  assert.match(shellCss, /\.shell__brand--builder \.shell__logo\s*\{[\s\S]*?filter:\s*drop-shadow/)
  assert.match(shellCss, /\.shell__brand--builder \.shell__wordmark\s*\{[\s\S]*?text-shadow:/)
})

test('entry assembles over a stationary Standard surface, compositor-only, instant under reduced motion (v3)', () => {
  // v3: entry is an opaque transform-only DEAL-IN keyed to the transient
  // .shell--builder-entering class, applied per-wrapper via data-mode-motion
  // (never the permanent .shell__view--paned). The Standard wrapper is the
  // stationary underlay and carries no competing scale animation.
  const panedBase = css.match(/\.shell__view--paned \{[\s\S]*?\n\}/)?.[0] || ''
  assert.doesNotMatch(panedBase, /animation:/)
  assert.doesNotMatch(panedBase, /transition:/)
  assert.match(css, /\.shell--builder-entering\s*\n\.shell__view\[data-mode-motion="deal-in"\] \{[\s\S]*?animation:\s*\n?\s*shell-mode-deal-in/)
  assert.match(css, /\.shell--builder-entering\s*\n\.shell__view\[data-mode-motion="deal-in"\] \{[\s\S]*?z-index:\s*2/,
    'every moving pane must paint above the stationary underlay regardless of app/chat DOM order')
  assert.doesNotMatch(css, /shell-mode-settle/)
  // Deal-in is transform-only: an opaque pane physically covers the retained single
  // screen as it arrives instead of exposing pane structure before fading content in.
  const dealIn = css.match(/@keyframes shell-mode-deal-in\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(dealIn, /translate3d\(var\(--mode-offset-x\), var\(--mode-offset-y\), 0\)/)
  assert.doesNotMatch(dealIn, /opacity:/)
  assert.doesNotMatch(dealIn, /box-shadow|border-radius|filter|clip/)
  assert.match(css, /--ease-mode-arrive:\s*cubic-bezier\(0\.33, 1, 0\.68, 1\)/,
    'entry keeps readable cubic-out travel without braking into the shared seam')
  assert.match(css, /--ease-mode-promote:\s*cubic-bezier\(0\.2, 0\.82, 0\.2, 1\)/,
    'the accepted exit promotion curve stays unchanged')
  // The old dead .workspace--resizing selector is gone entirely.
  assert.doesNotMatch(css, /workspace--resizing/)
  assert.doesNotMatch(css, /shell-pane-deal|shell-strip-deal-in|shell-pane-settle/)
  // Reduced motion: any data-mode-motion element gets no beat (defensive parity —
  // the controller never even arms one).
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(reduced, /\.shell \[data-mode-motion\] \{ animation: none !important; \}/)
})

test('builder single-leaf: the strip deals with its pane, entry through the ONE controller (item 3)', () => {
  // The strip is the builder surface: visible in the effective builder world even
  // at one leaf, and never in single mode or while immersive is covering the
  // preserved builder world.
  assert.match(shell, /const tabStripVisible = !immersiveActive\s*\n?\s*&& \(SPLITS \? effectiveViewMode === 'panes' : tabStripEngaged\)\s*\n?\s*&& openTabs\.length >= 1/)
  const handler = shell.match(/const handleToggleViewMode = useCallback\(\(cause\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  // v2: the handler builds the latched plan (deriveEnter/ExitPlan from the
  // projection) and dispatches the controller beat + the durable flip in the SAME
  // handler (INV 2/3). No Shell timer, no per-pane role plumbing.
  // The plan derives from the SETTLED post-flip state (the synchronous reducer
  // preview): the durable flip and the null-slot home resolution land first, so
  // the beat animates toward the surface single mode will actually paint.
  assert.match(handler, /deriveEnterPlan\(\{[\s\S]*?workspace: settled, projection, contentRect,/)
  assert.match(handler, /mode\.toggle\(\{ cause, presentation \}\)/)
  assert.match(handler, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE', mode: 'toggle' \}\)/)
  assert.match(shell, /modeMachine\.transitionRootClass\(modeState/)
  // The single-pane nav strip carries the beat motion so it deals WITH its pane.
  assert.match(shell, /data-mode-motion=\{navMotion \? navMotion\.motion : undefined\}/)
  // It is INERT throughout either direction (not just under the drawer), so a tap
  // on an in-flight strip cannot re-target the transition.
  assert.match(shell, /className="shell__tabstrip"[\s\S]*?inert=\{navigationSurfaceOpen \|\| modeBeatActive\}/)
  // CSS: a strip deals in with its pane on enter (shared with the WorkspaceChrome
  // strips via .shell__tabstrip[data-mode-motion]).
  assert.match(css, /\.shell--builder-entering \.shell__tabstrip\[data-mode-motion="deal-in"\] \{[\s\S]*?shell-mode-deal-in/)
  // Reduced motion drops any beat.
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(reduced, /\.shell \[data-mode-motion\] \{ animation: none !important; \}/)
})

test('leaving builder plays the INVERSE deal: compositor-only promote/deal-out, decisive (item 1)', () => {
  const handler = shell.match(/const handleToggleViewMode = useCallback\(\(cause\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  // v2: the latched plan owns classification (promote a genuinely-shared pane vs
  // reveal the single world underneath), shared timing, and the FLIP rects — the
  // handler no longer computes settlePaneId / leavingPaneIds / dealMultiPane itself.
  assert.match(handler, /const leavingBuilder = ws\.viewMode !== 'single'/)
  assert.match(handler, /deriveExitPlan\(\{[\s\S]*?workspace: settled, projection, contentRect,/)
  // The durable flip goes through the workspace boundary BEFORE plan derivation. That
  // boundary owns a null-slot New Chat request, so the destination remains
  // home:new-chat through the beat without a toggle-specific policy patch.
  assert.match(handler, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE', mode: 'toggle' \}\)[\s\S]*?const settled/)
  assert.match(shell, /enteredEmptySingleScreen\(\s*prev\.ws, next\.ws/)
  assert.match(shell, /requestEmptySingleNewChatRef\.current\?\.\(\)/)
  assert.doesNotMatch(handler, /requestEmptySingleNewChat/)
  // M2: the exit plan is fed the honest single-world destination state (a suspended
  // Settings takeover / a retained immersive holder), so it reveals to Settings or
  // classifies immersive instant instead of promoting/revealing the covered slot.
  assert.match(handler, /settingsDestination: settingsDestinationRef\.current/)
  assert.match(handler, /immersiveHolderId: immersiveHolderRef\.current/)
  assert.doesNotMatch(handler, /settlePaneId|leavingPaneIds|dealMultiPane|multiPaneRef/)
  // Held tiled while the beat runs — from the ONE descriptor (INV 4).
  assert.match(shell, /const effectiveViewMode = modeMachine\.effectiveViewMode\(modeState/)
  // The old data-pane-role + renderTabRects wrapper-widen are GONE: panes hold their
  // tiled rect and animate transform (data-mode-motion + inline --flip/--mode vars).
  assert.doesNotMatch(shell, /data-pane-role/)
  assert.doesNotMatch(shell, /renderTabRects/)
  assert.match(shell, /data-mode-motion=\{motion \? motion\.motion : undefined\}/)
  // The world-reveal underlay paints full-bleed beneath the deal (INV 5).
  assert.match(shell, /shell__view--exit-underlay/)
  // M2: the Settings surface itself can be that underlay (a suspended takeover is the
  // honest destination), painted full-bleed beneath the deal via its mounted-hidden
  // wrapper rather than snapping over a revealed slot at completion.
  assert.match(shell, /const settingsUnderlay = isUnderlay\(SETTINGS_KEY\)/)
  // CSS v3: promote FLIPs to full-bleed; siblings scatter to projection-derived edges.
  assert.match(css, /\.shell--builder-exiting\s*\n\.shell__view\[data-mode-motion="promote"\] \{[\s\S]*?shell-mode-promote/)
  assert.match(css, /\.shell--builder-exiting\s*\n\.shell__view\[data-mode-motion="deal-out"\] \{[\s\S]*?shell-mode-deal-out/)
  const dealOut = css.match(/@keyframes shell-mode-deal-out\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(dealOut, /translate3d\(var\(--mode-offset-x\), var\(--mode-offset-y\), 0\)/)
  assert.match(dealOut, /opacity: 0/)
  assert.doesNotMatch(dealOut, /box-shadow|border-radius|filter|clip/)
  assert.match(css, /--ease-mode-leave:\s*cubic-bezier\(0\.4, 0, 0\.7, 0\.2\)/,
    'scatter keeps the accepted inverse departure curve')
  // The parent-chrome opacity fade is DELETED (strips deal with their panes now).
  assert.doesNotMatch(css, /\.shell--builder-exiting \.workspace__chrome \{[\s\S]*?opacity: 0/)
  // Reduced motion drops any beat.
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(reduced, /\.shell \[data-mode-motion\] \{ animation: none !important; \}/)
})

test('world reveal keeps its destination stationary and ready beneath one short slide', () => {
  const underlay = css.match(/\.shell__view--exit-underlay\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(underlay, /visibility:\s*visible/)
  assert.doesNotMatch(underlay, /animation:|transition:|transform:|opacity:/)
  assert.doesNotMatch(css, /shell-mode-destination-arrive|--mode-arrive/)
  const workspaceView = readFileSync(new URL('../workspaceView.js', import.meta.url), 'utf8')
  assert.doesNotMatch(workspaceView, /destinationMotion|DESTINATION_ARRIVE_NAME|exitArriveMs/)
  assert.doesNotMatch(shell, /arriveVars|--mode-arrive/)
})

test('the PROPOSED builder power-chrome is behind a default-OFF flag + root class', () => {
  // The flag only enables on the literal '1' (default off), read once at load.
  assert.match(paneModelSrc, /export const BUILDER_POWER_CHROME = \(\(\) => \{[\s\S]*?localStorage\.getItem\('mobius:builder-power'\) === '1'/)
  // Shell adds the root class only when the flag is on AND builder mode is active.
  assert.match(shell, /builderModeActive && paneModel\.BUILDER_POWER_CHROME \? ' shell--builder-power' : ''/)
  // The gated chrome: a power-rail under the bar + energized dividers.
  assert.match(shellCss, /\.shell--builder-power \.shell__bar\s*\{[\s\S]*?box-shadow/)
  assert.match(css, /\.shell--builder-power \.workspace__divider-bar/)
})

test('the logo keeps the stable "Toggle navigation" name; gesture rides aria-description + live region', () => {
  // The accessible NAME stays stable (drawer semantics + e2e selectors depend on
  // it); the hold/keyboard path is a supplementary aria-description, and mode state
  // rides a polite live region (not a conflicting aria-pressed).
  assert.match(shellBrand, /aria-label="Toggle navigation"/)
  assert.match(shellBrand, /aria-description=\{splitsEnabled\s*\n?\s*\? 'Hold or press Shift\+Enter for builder mode'/)
  assert.match(shellBrand, /role="status" aria-live="polite"/)
  assert.match(shellBrand, /builderModeActive \? 'Builder mode' : 'Single screen'/)
})

test('mobile tabs require a hold before dragging while the strip preserves pinch zoom', () => {
  assert.match(shellCss, /\.shell__tabstrip\s*\{[\s\S]*?touch-action:\s*pan-x pinch-zoom/)
  assert.match(shellCss, /\.shell__tab-open\[data-drag-key\]\s*\{[\s\S]*?touch-action:\s*pinch-zoom/)
  assert.doesNotMatch(shellCss, /data-touch-drag-handle/)
  assert.doesNotMatch(paneStrip, /data-touch-drag-handle/)
  assert.doesNotMatch(paneStrip, /GripVertical|shell__tab-drag-handle/)
  assert.equal((paneStrip.match(/data-drag-key=\{dragKey\}/g) || []).length, 1,
    'the tab button is the one drag source')
  assert.match(drawerCss, /\.drawer__row \.drawer__item\[data-drag-key\]\s*\{[\s\S]*?touch-action:\s*pan-y pinch-zoom/)
  assert.match(dragBinding, /if \(sourceKind === 'tab'\) arm\(\)/)
  assert.match(dragBinding, /touchMoveIntent\(dx, dy, sourceKind\)/)
  assert.match(dragBinding, /scrollStripEl\.scrollLeft \+= previousPoint\.x - ev\.clientX/)
  assert.doesNotMatch(dragBinding, /addEventListener\('touchmove'/)
})

// iOS/WebKit does not implement the touch-action pan-* keywords (WebKit 133112),
// so `touch-action: pan-y pinch-zoom` is dropped there entirely and cannot
// reserve the horizontal axis for swipe-to-close. The gesture must therefore be
// CLAIMED: a non-passive touchmove listener that cancels the event once the pan
// is recognized. React's own onTouch* props are passive at the root, so binding
// through them silently made the drawer undismissable by swipe on iPhone.
test('drawer swipe-to-close claims the gesture with a non-passive touchmove listener', () => {
  assert.match(drawer, /addEventListener\('touchmove', move, \{ passive: false \}\)/)
  assert.doesNotMatch(drawer, /onTouchMove=\{/,
    "React's touch props are passive — preventDefault from them is a no-op")
  // The claim itself, plus the sticky per-gesture ownership flag it reads.
  assert.match(drawer, /if \(dx < 0 && isHorizontalSwipe\) panningRef\.current = true/)
  assert.match(drawer, /if \(!panningRef\.current\) return\s*\n[\s\S]{0,400}?e\.preventDefault\(\)/)
})

test('workspace tabs spend their chrome on names rather than redundant kind icons', () => {
  assert.doesNotMatch(paneStrip, /shell__tab-kind|AppWindow|MessageSquare|Settings/)
  assert.match(shellCss, /\.shell__tab-text\s*\{[\s\S]*?flex:\s*1[\s\S]*?max-width:\s*128px/)
})

test('an active overflowing chat title cycles once, then becomes idle', () => {
  assert.match(paneStrip, /new ResizeObserver\(measure\)/)
  assert.match(paneStrip, /!active \|\| !focused \|\| tab\.kind !== 'chat'/)
  assert.match(paneStrip, /\}, \[active, focused, label, tab\.kind\]\)/,
    'only the focused active tab should retain a ResizeObserver')
  assert.match(paneStrip, /title\.style\.setProperty\('--tab-title-shift'/)
  assert.match(paneStrip, /title\.style\.setProperty\('--tab-title-duration'/)
  assert.match(paneStrip, /Math\.round\(shift \* TITLE_CYCLE_MS_PER_PX\)/)
  assert.doesNotMatch(paneStrip, /TITLE_CYCLE_MIN_MS/,
    'clipped titles must not share a fixed duration; distance owns the cadence')
  assert.doesNotMatch(paneStrip, /TITLE_CYCLE_MAX_MS|Math\.min/,
    'long titles must not accelerate through a duration cap')
  assert.match(paneStrip, /const TITLE_CYCLE_MS_PER_PX = 1000 \/ 12/)
  assert.match(paneStrip, /className="shell__tab-text-inner"/)
  const cycle = shellCss.match(/\.shell__tabstrip:not\(\.workspace__strip\)[\s\S]*?shell-tab-title-cycle var\(--tab-title-duration\) linear 700ms 1 both/)?.[0] || ''
  assert.match(cycle, /\.workspace__strip--focused/)
  assert.doesNotMatch(cycle, /infinite/)
  const keyframes = shellCss.match(/@keyframes shell-tab-title-cycle\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(keyframes, /0%, 5% \{ transform: translate3d\(0, 0, 0\)/,
    'the opening rest stays short even when the travel duration scales')
  assert.match(keyframes, /95%, 100% \{ transform: translate3d\(0, 0, 0\)/,
    'the one pass returns to the beginning and rests there')
  assert.match(shellCss, /\.shell__tab-text-inner \{ animation: none !important; \}/)
})

test('the pane focus action uses one unambiguous accessible state contract', () => {
  assert.match(paneStrip, /const label = focused \? 'Show all panes' : 'Focus pane'/)
  assert.match(paneStrip, /aria-label=\{label\}/)
  assert.doesNotMatch(paneStrip, /aria-pressed/,
    'a button whose label changes with the action must not also announce a toggle state')
})

test('the pane focus action stays compact at the far edge and reachable on overflow', () => {
  const stripRule = css.match(/\.workspace__strip\s*\{[\s\S]*?\n\}/)?.[0] || ''
  const focusRule = css.match(/\.workspace__pane-focus\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(stripRule, /padding-inline-end:\s*0/,
    'the strip owns removal of its trailing gutter')
  assert.match(focusRule, /position:\s*sticky/)
  assert.match(focusRule, /right:\s*0/)
  assert.match(focusRule, /flex:\s*0 0 28px/)
  assert.match(focusRule, /margin-left:\s*auto/,
    'free strip width belongs to the tabs; the focus action stays at the far edge')
  assert.doesNotMatch(focusRule, /margin-right|translateX/,
    'the control should not compensate for padding owned by its strip')
})

test('overflowing strips keep native pan and add a no-chrome wheel path', () => {
  assert.match(paneStrip, /export function scrollStripWheel\(e\)/)
  assert.match(paneStrip, /Math\.abs\(e\.deltaX\) >= Math\.abs\(e\.deltaY\)/)
  assert.match(paneStrip, /strip\.scrollLeft \+= e\.deltaY \* scale/)
  assert.match(paneStrip, /onWheel=\{scrollStripWheel\}/)
  assert.match(shell, /onWheel=\{scrollStripWheel\}/)
})

test('navigation surfaces keep the brand close path while the workspace is inert', () => {
  const header = shell.match(/<header className="shell__bar"[^>]*>/)?.[0] || ''
  assert.doesNotMatch(header, /inert=/)
  assert.match(shell, /const navigationSurfaceOpen = modalDrawerOpen/)
  assert.doesNotMatch(shell, /const navigationSurfaceOpen = .*apps/,
    'the canonical Apps tab is workspace content, not a modal navigation surface')
  assert.match(shell, /<main className="shell__content"[^>]*inert=\{navigationSurfaceOpen\}/)
  assert.match(shellBrand, /aria-expanded=\{navigationOpen\}/)
  assert.match(shell, /drawerOpen \? closeDrawer\(\) : openDrawer\(\)/)
})

test('the Apps tab never disables the mobile drawer layered above it', () => {
  assert.doesNotMatch(drawer, /drawer__body" inert=/,
    'workspace content is inert while the drawer is open; the drawer must stay interactive')
  assert.doesNotMatch(drawer, /interactionLocked \|\| appsActive/,
    'Apps is a tab underneath navigation, not a modal owner of Escape')
  assert.match(drawer, /appsActive \? ' drawer__item--active'/)
  assert.match(shell, /appsActive=\{appsVisibleAsTab\}/)
})

test('opening navigation is presentation-only and never refetches whole lists', () => {
  assert.doesNotMatch(shell, /if \(navigationOpen\) \{ refreshApps\(\); refreshChats\(\) \}/)
  assert.match(shell, /ev\.type === 'app_updated'[\s\S]*?ev\.type === 'app_created'[\s\S]*?ev\.type === 'app_preview_ready'[\s\S]*?refreshApps\(\)/,
    'app lifecycle events still own their authoritative refresh')
  assert.match(shell, /ev\.type === 'chat_run_finished'[\s\S]*?refreshChats\(\)/,
    'chat lifecycle events still own their authoritative refresh')
})

test('chat drawer dots distinguish active work from unseen completion', () => {
  assert.match(
    shell,
    /ev\.type === 'chat_run_started'[\s\S]*?markStreamingStart\(ev\.chatId\)/,
    'a started run must raise the active-work dot',
  )
  assert.match(
    shell,
    /ev\.type === 'chat_run_finished'[\s\S]*?!visibleChatIdsRef\.current\.has\(String\(chatId\)\)[\s\S]*?setAttentionChatIds/,
    'a finished run must raise attention only while the chat is not visible',
  )
  assert.match(
    shell,
    /for \(const cid of visibleChatIds\) clearChatAttention\(cid\)/,
    'viewing a chat must clear its completion dot',
  )
  assert.match(
    drawer,
    /streaming \? \([\s\S]*?drawer__streaming-dot[\s\S]*?: attention \? \([\s\S]*?drawer__attention-dot/,
    'active work must take precedence over unseen completion in a row',
  )
  assert.match(drawerCss, /\.drawer__streaming-dot\s*\{[\s\S]*?background:\s*var\(--accent\)/)
  assert.match(drawerCss, /\.drawer__attention-dot\s*\{[\s\S]*?border:\s*1\.5px solid var\(--green\)/)
})

test('live preview reveal keeps the workspace controller distinct from device mode', () => {
  assert.match(shell, /const deviceMode = paneModel\.modeForRect\(contentRect\)/)
  assert.match(shell, /mode\.toggle\(\{ cause: 'auto', to: 'panes' \}\)/)
  assert.match(shell, /resolveWorkspaceRequests\(ws, requests, \{[\s\S]*?mode: deviceMode,/)
  assert.doesNotMatch(shell, /const mode = paneModel\.modeForRect\(contentRect\)/)
})

test('large drawer lists memoize ordering and row actions without changing row ownership', () => {
  assert.match(drawer, /useMemo\(\(\) => buildDrawerSections\(chats, apps\), \[chats, apps\]\)/)
  assert.match(drawer, /const filteredApps = useMemo\(/)
  assert.match(drawer, /const rowActions = useMemo\(/)
  assert.match(drawer, /const DrawerRow = memo\(function DrawerRow/)
  assert.match(drawer, /visibleRecents\.map\(\(\{ kind, item \}\)[\s\S]*?item=\{item\}[\s\S]*?actions=\{rowActions\}/)
  assert.match(drawer, /item=\{app\}[\s\S]*?actions=\{rowActions\}/)
  assert.doesNotMatch(drawer, /onSelect=\{\(\) => on(?:Chat|App)/)
})

test('mixed recents reserve artwork for apps and separate sections without a second type scale', () => {
  assert.match(drawer, /kind === 'app'[\s\S]*?<AppIcon/)
  assert.doesNotMatch(drawer, /drawer__chat-icon|<Chat\b|<Clock\b|<PinFilled\b/)
  assert.match(
    drawerCss,
    /\.drawer__label\s*\{[\s\S]*?font-size:\s*14px[\s\S]*?font-weight:\s*600[\s\S]*?color:\s*var\(--text\)[\s\S]*?padding:\s*6px 12px[\s\S]*?margin:\s*12px 0 4px/,
  )
  assert.match(
    drawerCss,
    /\.drawer__item\s*\{[\s\S]*?font-size:\s*14px/,
  )
  assert.match(
    drawerCss,
    /@media \(min-width: 1024px\)[\s\S]*?\.drawer__item,[\s\S]*?font-size:\s*15px[\s\S]*?\.drawer__label\s*\{[\s\S]*?font-size:\s*15px/,
  )
  assert.doesNotMatch(
    drawerCss,
    /\.drawer__row \.drawer__item\s*\{[^}]*font-size:/,
  )
})

test('New chat and Apps share one compact navigation rhythm', () => {
  assert.match(
    drawerCss,
    /\.drawer__item--new\s*\{[\s\S]*?margin:\s*0;/,
  )
  assert.match(
    drawerCss,
    /\.drawer__scroll--navigation\s*\{[\s\S]*?margin-top:\s*0;[\s\S]*?padding-top:\s*0;/,
  )
})

test('the Möbius header keeps its phone divider but flows into desktop navigation', () => {
  assert.match(
    shellCss,
    /\.shell__bar\s*\{[\s\S]*?border-bottom:\s*1px solid var\(--border-light\);/,
  )
  assert.match(
    shellCss,
    /@media \(min-width: 1024px\)[\s\S]*?\.shell__bar\s*\{[\s\S]*?border:\s*0;/,
  )
  assert.match(
    shellCss,
    /\.shell--drawer-docked \.shell__bar\s*\{[\s\S]*?border-bottom:\s*0;/,
  )
})

test('row-owned context menus need no permanent kebab or hidden anchor', () => {
  assert.match(drawer, /<DrawerItemActionMenu[\s\S]*?itemKind=\{kind\}/)
  assert.doesNotMatch(
    drawer,
    /triggerHidden|drawer__menu-anchor|drawer__more|DotsVerticalMoreMenu/,
  )
  assert.doesNotMatch(drawerCss, /drawer__menu-anchor|drawer__more/)
  assert.match(drawer, /onContextMenu=\{openItemMenu\}/)
  assert.match(
    drawer,
    /event\.key === 'ContextMenu' \|\| \(event\.shiftKey && event\.key === 'F10'\)/,
  )
  assert.match(drawer, /if \(openMenu\) return[\s\S]*?onClose\?\.\(\)/,
    'Escape must close a row menu before dismissing the mobile drawer beneath it')
})

test('chat deletion is immediate while app deletion still requires confirmation', () => {
  assert.match(
    drawerItemActionMenu,
    /function handleDeleteAction\(\)[\s\S]*?itemKind === 'chat'[\s\S]*?run\(onDelete, \{ restoreFocus: false \}\)[\s\S]*?return[\s\S]*?setConfirmation\('delete'\)/,
  )
  assert.match(
    drawerItemActionMenu,
    /className="drawer__item-action-item drawer__item-action-item--danger"\s*\n\s*onClick=\{handleDeleteAction\}/,
  )
  assert.match(
    drawerItemActionMenu,
    /confirmation === 'delete-data'[\s\S]*?confirmation === 'delete'/,
    'app and app-data deletion must retain their confirmation paths',
  )
})

test('drawer row menus use one semantic context-menu path across pointer types', () => {
  assert.match(drawer, /function openItemMenu\(event\)[\s\S]*?actions\.toggleMenu\(kind, id, true, surface,/)
  assert.equal((drawer.match(/onContextMenu=\{openItemMenu\}/g) || []).length, 2,
    'app cards, app rows, and chat rows must share one opening function')
  assert.match(dragBinding, /srcEl\.dispatchEvent\(new window\.MouseEvent\('contextmenu'/)
  assert.doesNotMatch(
    dragBinding,
    /srcEl\.closest\('\.drawer__row'\)\?\.querySelector\('\.drawer__more'\)\?\.click\(\)/,
    'touch hold must not depend on a synthetic trigger click',
  )
})

test('a secondary-button release cannot immediately select a flipped drawer menu item', () => {
  assert.match(drawer, /event\.type === 'contextmenu' && secondaryReleaseCleanupRef\.current/)
  assert.match(drawer, /event\.pointerType !== 'mouse' \|\| event\.button !== 2/)
  assert.match(drawer, /window\.addEventListener\('pointerup', onSecondaryPointerUp, true\)/)
  assert.match(drawer, /upEvent\.pointerId !== pointerId \|\| upEvent\.button !== 2/)
  assert.match(drawer, /cleanup\(\)[\s\S]*?actions\.toggleMenu\(kind, id, true, surface, placement\)/)
  assert.match(drawer, /timer = setTimeout\(cleanup, 1500\)/)
})

test('launcher cards and drawer rows share the same long-press threshold and movement slop', () => {
  assert.match(drawer, /import \{ DRAWER_HOLD_MS, PRE_HOLD_MOVE_PX \} from '\.\.\/Shell\/dragController\.js'/)
  assert.match(drawer, /\}, DRAWER_HOLD_MS\)/)
  assert.match(drawer, /> PRE_HOLD_MOVE_PX/)
  assert.doesNotMatch(drawer, /520/)
})

test('double-click edits a drawer row name instead of duplicating its context menu', () => {
  assert.match(drawer, /onDoubleClick=\{event => \{[\s\S]*?actions\.startRename\(kind, id, surface\)/)
})

test('the Settings surface responds to PANE width via a query container', () => {
  const settingsCss = readFileSync(
    new URL('../../SettingsView/SettingsView.css', import.meta.url), 'utf8',
  )
  const urmCss = readFileSync(
    new URL('../../SettingsView/UpdateReviewModal.css', import.meta.url), 'utf8',
  )
  // The pane-sized wrapper is the query container.
  const wrap = shellCss.match(/\.shell__settings-view\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(wrap, /container:\s*settings \/ inline-size/)
  // SettingsView reads that container, not the viewport, so a narrow builder pane
  // in a wide screen gets the compact layout (the @media miss the design names).
  assert.match(settingsCss, /@container settings \(max-width: 620px\)/)
  assert.match(settingsCss, /@container settings \(max-width: 400px\)/)
  assert.doesNotMatch(settingsCss, /@media \(max-width: 620px\)/)
  // The update-review modal stays a FIXED takeover (design: not reclassified to a pane).
  assert.match(urmCss, /\.urm__overlay\s*\{[\s\S]*?position:\s*fixed/)
})

test('a manual platform reconcile refreshes the persistent Settings surface', () => {
  assert.match(shell, /const \[settingsRefreshToken, setSettingsRefreshToken\] = useState\(0\)/)
  assert.match(
    shell,
    /if \(ev\.type === 'shell_apply_now'\) \{\s*setSettingsRefreshToken\(token => token \+ 1\)/,
  )
  assert.match(shell, /active=\{!settingsUnderlay && \(settingsFullBleed \|\| !!settingsPaned\)\}/)
  assert.match(shell, /refreshToken=\{settingsRefreshToken\}/)
  assert.match(settingsView, /active = true,\s*refreshToken = 0,/)
  assert.match(
    settingsView,
    /useEffect\(\(\) => \{\s*if \(active\) refreshPlatform\(\)\s*\}, \[active, refreshPlatform, refreshToken\]\)/,
  )
})

test('the builder no-full-screen invariant scopes to DESTINATIONS, not transient dialogs (§2)', () => {
  // The invariant governs navigable destinations (Settings, takeover views,
  // immersive), NOT dismissible dialogs layered over the workspace. Those stay
  // fixed modals with their own dismiss and are out of the invariant's scope.
  const navSrc = readFileSync(new URL('../../../hooks/useNavigation.js', import.meta.url), 'utf8')
  assert.match(navSrc, /DESTINATIONS, NOT DIALOGS/)
  const walkthrough = readFileSync(
    new URL('../../Walkthrough/WalkthroughOverlay.jsx', import.meta.url), 'utf8',
  )
  const urmCss = readFileSync(
    new URL('../../SettingsView/UpdateReviewModal.css', import.meta.url), 'utf8',
  )
  // First-use guidance is now a non-modal region layered over the live shell,
  // with an explicit dismiss action; update review remains a fixed modal.
  assert.match(walkthrough, /role="region"/)
  assert.match(walkthrough, /aria-label="Dismiss welcome"/)
  assert.doesNotMatch(walkthrough, /aria-modal="true"/)
  assert.match(urmCss, /\.urm__overlay\s*\{[\s\S]*?position:\s*fixed/)
})

test('Shell threads the (drag-preview) viewMode into the content derivation and the per-pane chat gate', () => {
  // effectiveViewMode is the ONE descriptor derivation (INV 4): 'panes' during a
  // single-mode drag preview OR the builder EXIT beat, committed mode otherwise.
  assert.match(shell, /const effectiveViewMode = modeMachine\.effectiveViewMode\(modeState/)
  assert.match(shell, /viewMode: effectiveViewMode/)
  // The single-mode drag arms the 'drag-preview' phase through the controller by id
  // (INV 5), and the drop's committed 'panes' is picked up by the committedMode
  // reconcile — no separate SET_VIEW_MODE on commit.
  assert.match(shell, /dragPreviewIdRef\.current = mode\.dragArm\(/)
  assert.match(shell, /mode\.dragCancel\(dragPreviewIdRef\.current\)/)
  assert.match(shell, /const \{ multiPane, single, focusedActiveKey, fullBleedKey, visibleAppIds \}/)
  // Each retained owner paints only in its own world. Standard and Builder can
  // retain the same chat without sharing geometry or activating both runtimes.
  assert.match(shell, /const standardOwner = world === STANDARD_CHAT_WORLD/)
  assert.match(shell, /const builderPainted = !standardOwner[\s\S]*effectiveViewMode === 'panes'/)
  assert.match(shell, /visible=\{surfaceVisible && chatPanesVisible && role !== 'held'\}/)
})

test('DRAG IS BUILDING: arming in single mode unfolds a builder preview; any drop commits panes', () => {
  // No drag-deny anymore — arming always proceeds; a single-mode arm turns on the
  // render-only builder preview (Shell flips it via onPreviewBuilder / effectiveViewMode).
  assert.doesNotMatch(dragBinding, /dragArmingBlocked|onDragBlocked/)
  assert.match(dragBinding, /if \(workspaceStateRef\.current\.ws\.viewMode === 'single'\) onPreviewBuilder\?\.\(true\)/)
  assert.match(dragBinding, /onPreviewBuilder\?\.\(false\)/) // cleared on cleanup (commit AND cancel)
  // ANY single-mode drop commits builder mode (folds in the former single-leaf flip);
  // the flip is folded into OPEN_TAB_AT so ONE undo reverts both tree and viewMode.
  assert.match(dragBinding, /const flipToPanes = before\.viewMode === 'single'/)
  assert.match(dragBinding, /flipViewMode: flipToPanes \? 'panes' : null/)
  assert.doesNotMatch(dragBinding, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE'/)
  // v2 deleted the Settings mode-conversion: a builder Settings tab survives the
  // flip, so a drop-into-builder no longer routes any overlay<->tab conversion.
  assert.doesNotMatch(dragBinding, /convertSettingsForModeTransition/)
  // §8: "committed" is the ACTUAL workspace change, not a stale lit-zone flag.
  assert.match(dragBinding, /return workspaceStateRef\.current\.ws !== before/)
  assert.match(dragBinding, /if \(moveRAF\) \{[\s\S]*?cancelAnimationFrame\(moveRAF\)[\s\S]*?doMoveWork\(\)/)
  assert.match(dragBinding, /const didCommit = curZone \? commitDrop\(\) : false/)
  // The drag-deny shake is gone from the CSS too.
  assert.doesNotMatch(shellCss, /is-vibrating|shell-brand-shake|shell-brand-pulse/)
})

test('the builder preview cannot outlive its drag session past one visibility boundary', () => {
  // The render-only builder preview (dragPreviewBuilder) is a SHARED effectiveViewMode
  // override; a session that strands it true wedges the workspace tiled forever. Two
  // guards keep it bounded:
  // (1) SOURCE — pagehide joins the per-session teardown, so a BFCache freeze that
  //     fires no pointercancel/blur/visibilitychange still cancels the drag.
  assert.match(dragBinding, /const onPageHide = \(\) => cleanup\(\{ suppressClick: armed \|\| scrolling \}\)/)
  assert.match(dragBinding, /window\.addEventListener\('pagehide', onPageHide\)/)
  assert.match(dragBinding, /window\.removeEventListener\('pagehide', onPageHide\)/)
  // (2) BACKSTOP — a persistent foreground reconcile force-cleans any session still
  //     standing at a visible/pageshow edge (its going-out teardown was skipped) and
  //     asserts the override is off. suppressClick:true so a late click after the
  //     force-clean cannot activate the source (finding 4). It acts on the OPPOSITE
  //     edge from the teardown, so the two never double-handle, and it never cancels
  //     a genuinely live drag (a live drag never receives these edges).
  assert.match(dragBinding, /function reconcileStaleSession\(\) \{[\s\S]*?activeCleanup\?\.\(\{ suppressClick: true \}\)[\s\S]*?onPreviewBuilder\?\.\(false\)/)
  assert.match(dragBinding, /if \(document\.visibilityState === 'visible'\) reconcileStaleSession\(\)/)
  assert.match(dragBinding, /window\.addEventListener\('pageshow', reconcileStaleSession\)/)
  assert.match(dragBinding, /document\.addEventListener\('visibilitychange', onForegroundVisible\)/)
  // Both foreground listeners are torn down with the effect.
  assert.match(dragBinding, /window\.removeEventListener\('pageshow', reconcileStaleSession\)/)
  assert.match(dragBinding, /document\.removeEventListener\('visibilitychange', onForegroundVisible\)/)
  // (3) NEXT-INTERACTION — a visible->visible steal (partial occlusion / split-screen)
  //     fires NEITHER edge; the next pointerdown reconciles a standing session whose
  //     pointer is dead (no live capture), then proceeds. Pointer identity is NOT a
  //     liveness signal because mobile reuses ids across sequential gestures. This
  //     newer boundary needs no old-gesture click guard; adding one would eat the
  //     fresh tap on the same drawer row. A live drag keeps its capture, so it stays.
  assert.match(dragBinding, /function standingSessionPointerIsLive\(\) \{[\s\S]*?hasPointerCapture\?\.\(activePointerId\)/)
  assert.match(dragBinding, /if \(!standingSessionPointerIsLive\(\)\) \{\s*activeCleanup\(\)/)
  assert.match(dragBinding, /clearPendingSourceClick\?\.\(\)[\s\S]*?if \(activeCleanup\)/)
  // The invariant now spans one boundary OR one subsequent interaction.
  assert.match(dragBinding, /may outlive its session by at most ONE visibility\/foreground boundary,\s*\n?\s*\/\/ or at most one subsequent user interaction/)
})

test('the splits kill-switch forces the single-pane fallback so a rolled-back panes blob is not un-exitable', () => {
  // The tiled render (chromeActive) is flag-INDEPENDENT, but both exit controls
  // (the logo gesture + Shift+Enter) are flag-GATED. So a rolled-back client that
  // persisted a 'panes' blob and then had WORKSPACE_SPLITS disabled would restore
  // TILED with no way to reach single ("cannot reach single mode", survives reload).
  // coerceViewMode — run by normalize() on every parse/restore — forces 'single'
  // when splits are off, delivering the kill-switch's documented single-pane fallback
  // (the tree is preserved; re-enabling splits restores the panes).
  assert.match(paneModelSrc, /function coerceViewMode\(mode\) \{\s*\n\s*if \(!WORKSPACE_SPLITS_ENABLED\) return 'single'/)
})

test('workspace focus, drag label, and cancel visuals remain coherent', () => {
  // V4: the FOCUSED pane's active pill softens the base full-accent border so the 2px
  // underline is what carries focus (the border used to mask it).
  const focused = css.match(/\.workspace__strip--focused \.shell__tab--active \{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(focused, /box-shadow: inset 0 -2px 0 0 var\(--accent\)/)
  assert.match(focused, /border-color: color-mix\(in srgb, var\(--accent\) 45%, var\(--border-light\)\)/)
  // V5: pointer and tab-client measurements cross the scale boundary before the
  // preview/chip write layout pixels. The chip then clamps against that same
  // layout viewport so its label never clips at the right edge.
  assert.match(dragBinding, /return clientPointToLocal\(\{ x: clientX, y: clientY \}, box\.rect, box\.localSize\)/)
  assert.match(dragBinding, /left: toLocal\(r\.left, r\.top, box\)\.x/)
  assert.match(dragBinding, /chipOffset\(toViewportLayout\(clientX, clientY\), isTouch\)/)
  assert.match(dragBinding, /w: root\.offsetWidth \|\| root\.clientWidth \|\| rect\.width/)
  assert.match(dragBinding, /const viewportWidth = document\.documentElement\.offsetWidth\s*\n\s*\|\| document\.documentElement\.clientWidth\s*\n\s*\|\| window\.innerWidth/)
  assert.match(dragBinding, /const maxLeft = Math\.max\(margin, viewportWidth - chipWidth - margin\)/)
  assert.match(dragBinding, /Math\.max\(margin, Math\.min\(left, maxLeft\)\)/)
  // V6: a CANCELLED drag blurs the drag-origin row so its focus ring clears; a
  // committed drop keeps focus (the tab moved).
  assert.match(dragBinding, /if \(suppressClick && !committed\) srcEl\.blur\?\.\(\)/)
})

test('workspace drag batches geometry reads before frame writes', () => {
  assert.match(dragBinding, /chipWidth = chipEl\.offsetWidth \|\| 0/,
    'the drag label width is measured once when it becomes visible')
  const frame = dragBinding.match(/const doMoveWork = \(\) => \{[\s\S]*?\n      \}/)?.[0] || ''
  assert.ok(frame.indexOf('const box = contentBox()') >= 0)
  assert.ok(frame.indexOf('const box = contentBox()') < frame.lastIndexOf('positionChip(cx, cy, isTouch, key)'))
  assert.match(frame, /updateAutoScroll\(cx, cy, box\)/)
  assert.match(frame, /toLocal\(cx, cy, box\)/)
  assert.match(dragBinding, /measureTabs\(autoPaneId, box\)/,
    'auto-scroll shares its content rect across strip and pointer measurements')
})

// ── H1 (was M5): a slot app uninstalled while closed must not survive the first
// reconcile — BUT absence from the NetworkFirst list is not deletion evidence ─────
test('H1: the initial slot-app reconcile confirms absence with an authoritative 404 probe', () => {
  // The single-world slot app is pinned even while builder paints, so the present->
  // absent eviction (gated on seenAppIds) never fires for a slot app uninstalled
  // while the browser was CLOSED — it was never "seen present" this session. Its
  // one-shot check must NOT trust the /api/apps/ list's absence (NetworkFirst → a
  // stale SW cache fallback is indistinguishable from a live response); it probes the
  // AUTHORITATIVE per-app endpoint and deletes ONLY on a real 404, mirroring the chat
  // 404-probe (cancelled + stale guards).
  const effect = shell.match(/One-shot slot-app reconcile \(H1\)[\s\S]*?workspaceStateRef\]\)/)?.[0] || ''
  assert.ok(effect.length > 0, 'found the slot-app probe effect')
  assert.match(effect, /if \(!appsLiveFetched \|\| initialSlotReconciledRef\.current\) return/)
  assert.match(effect, /const slot = workspaceStateRef\.current\.ws\.singleScreen/)
  // Fast path: a slot app the live list already vouches for is skipped, no probe.
  assert.match(effect, /if \(apps\.some\(a => Number\(a\.id\) === Number\(slot\.id\)\)\) return/)
  // The authoritative per-app probe via the shared deletion-evidence contract, and
  // teardown ONLY on a 'deleted' verdict (a real 404).
  assert.match(effect, /probeDeletion\(`\/apps\/\$\{encodeURIComponent\(slotId\)\}`\)/)
  assert.match(effect, /if \(verdict !== 'deleted'\) return/)
  // Stale-guard: a slot change mid-probe must never delete the new slot.
  assert.match(effect, /const current = workspaceStateRef\.current\.ws\.singleScreen/)
  assert.match(effect, /Number\(current\.id\) !== Number\(slotId\)\) return/)
  // Cancelled-guard cleanup, like the chat cold-restore probe.
  assert.match(effect, /let cancelled = false/)
  assert.match(effect, /return \(\) => \{ cancelled = true \}/)
  // Close as deleted (the reducer clears the slot); the shared dispatch boundary,
  // tested below, owns the New Chat landing rather than this effect patching it.
  assert.match(effect, /reason: 'deleted'/)
  assert.doesNotMatch(effect, /requestEmptySingleNewChat/)
})

// The shared deletion-evidence contract both cold-restore probes route through: list
// absence is a HINT, an authoritative per-resource 404 is the only proof of deletion.
test('deletion-evidence contract: probeDeletion classifies 404 vs exists vs unknown', () => {
  const client = readFileSync(new URL('../../../api/client.js', import.meta.url), 'utf8')
  assert.match(client, /export async function probeDeletion\(path\)/)
  assert.match(client, /if \(res\.status === 404\) return 'deleted'/)
  assert.match(client, /if \(res\.ok\) return 'exists'/)
  assert.match(client, /return 'unknown'/)
  // Both cold-restore probes read the SAME contract (rhyme, not two copies).
  assert.match(shell, /probeDeletion\(`\/apps\//)
  assert.match(shell, /probeDeletion\(`\/chats\//)
})

// ── Round 4 item 3: the null slot is a first-class, deferred New Chat landing ──
test('round4-3: requestEmptySingleNewChat records a tokenized request and does NOT write the slot', () => {
  const fn = shell.match(/const requestEmptySingleNewChat = useCallback\(\(\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  assert.ok(fn.length > 0, 'found the request helper')
  // Guarded to an empty single slot; captures the reuse candidate from the
  // pre-transition active chat; records a monotonic token; NEVER writes a slot itself.
  assert.match(fn, /if \(!single \|\| ws\.singleScreen != null\) return/)
  assert.match(fn, /currentReusableEmptyChat\(chatsRef\.current/)
  assert.match(fn, /activeChatId: activeChatIdRef\.current/)
  assert.match(fn, /newChatRequestSeqRef\.current = token/)
  assert.match(fn, /pendingNewChatRef\.current = \{ token, candidateId/)
  assert.match(fn, /setPendingNewChatToken\(token\)/)
  assert.doesNotMatch(fn, /applyModeDestination|SET_SINGLE_SCREEN|chatsRef\.current\[0\]/)
})

test('round4-3: every reducer edge into an empty single screen uses one policy boundary', () => {
  const dispatch = shell.match(/const dispatchWorkspace = useCallback\(\(action\) => \{[\s\S]*?\}, \[\]\)/)?.[0] || ''
  assert.ok(dispatch.length > 0, 'found the workspace dispatch boundary')
  assert.match(dispatch, /workspaceReducer\(prev, action\)/)
  assert.match(dispatch, /enteredEmptySingleScreen\(\s*prev\.ws, next\.ws/)
  assert.match(dispatch, /prev\.ws, next\.ws, paneModel\.WORKSPACE_SPLITS_ENABLED/)
  assert.match(dispatch, /requestEmptySingleNewChatRef\.current\?\.\(\)/)
  // Explicit calls remain only for boot states that do not cross a reducer edge:
  // populated-history null restore and live-confirmed zero-chat bootstrap.
  const explicitCalls = shell.match(/\brequestEmptySingleNewChat\(\)/g) || []
  assert.equal(explicitCalls.length, 2)
  // A create response updates the chat list before its slot write. Boot must not
  // interpret that refresh as a second request and POST another empty row.
  assert.match(shell, /chats\.length > 0\s*&& pendingNewChatRef\.current == null/)
})

test('round4-3: the materialize watcher gates on an IDLE descriptor', () => {
  const effect = shell.match(/Deferred New Chat materialization watcher[\s\S]*?workspaceStateRef\]\)/)?.[0] || ''
  assert.ok(effect.length > 0, 'found the materialize watcher')
  // Deferred until the mode descriptor idles — a slot write mid-beat would drift the
  // exit signature and cancel the latched plan.
  assert.match(effect, /if \(modeState\.transition\) return/)
  assert.match(effect, /pending\.token !== pendingNewChatToken/)
  assert.match(effect, /if \(!single \|\| ws\.singleScreen != null\)/)
  assert.match(effect, /materializeNewChatHomeRef\.current\?\.\(pending\)/)
})

test('round4-3: materializeNewChatHome is stale-guarded and writes a history-free, focus-free slot', () => {
  const fn = shell.match(/async function materializeNewChatHome\(pending\) \{[\s\S]*?\n  \}/)?.[0] || ''
  assert.ok(fn.length > 0, 'found materializeNewChatHome')
  // Shares the ONE reuse-and-create policy with newChat.
  assert.match(fn, /resolveNewChatId\(\{ candidate \}\)/)
  // Stale-guard: token still current, then invalid destinations clear the request.
  // A live beat is a separate keep-and-resume branch, not a destructive clear.
  assert.match(fn, /newChatRequestSeqRef\.current !== pending\.token/)
  assert.match(fn, /latest\.resolvedChatId = chatId/)
  assert.match(fn, /if \(!single \|\| ws\.singleScreen != null\) \{[\s\S]*?pendingNewChatRef\.current = null/)
  assert.match(fn, /if \(modeTransitionRef\.current\) return/)
  assert.match(fn, /pending\.resolvedChatId = chatId/)
  // A request that supersedes an in-flight token gets one event-driven retry after
  // the older await releases; there is no interval/polling loop.
  assert.match(fn, /latest\.token !== pending\.token[\s\S]*?setMaterializeNewChatRevision/)
  assert.doesNotMatch(fn, /setInterval|setTimeout/)
  // offline/failed → keep the landing with a retry state, never chats[0].
  assert.match(fn, /if \(chatId == null\) \{[\s\S]*?setNewChatLandingFailure\(reason === 'offline' \? 'offline' : 'error'\)/)
  // The slot write is history-free (applyModeDestination pushes none) + preserveSettings,
  // and there is NO composer focus (a mode toggle must not summon the keyboard).
  assert.match(fn, /applyModeDestination\(\s*\{ view: 'chat', chatId, appId: null, paneId: ws\.focusedPaneId \},\s*\{ preserveSettings: true \}/)
  assert.doesNotMatch(fn, /requestComposerFocus|focusComposer/)
})

test('round4-3: resolveNewChatId is the shared reuse-and-create policy; newChat + materialize both use it', () => {
  assert.match(shell, /async function resolveNewChatId\(\{ candidate, draft, forceNew, exclude \} = \{\}\)/)
  // newChat consumes the shared resolver, optionally supplying the standard-mode
  // resume candidate rather than growing a second create path.
  const fn = shell.match(/async function newChat\([\s\S]*?\n  \}/)?.[0] || ''
  assert.ok(fn.length > 0, 'found newChat')
  assert.match(fn, /const \{ chatId, reason \} = await resolveNewChatId\(/)
  assert.doesNotMatch(fn, /api\.chats\.create|apiFetch\(\s*['"`]\/chats/)
})

test('round4-3: the New Chat landing renders for a null slot / reveal underlay and reuses ChatView empty visuals', () => {
  // The presentation key + its wiring.
  assert.match(workspaceViewSrc, /export const EMPTY_SINGLE_SURFACE_KEY = 'home:new-chat'/)
  assert.match(shell, /const newChatUnderlay = isUnderlay\(EMPTY_SINGLE_SURFACE_KEY\)/)
  assert.match(shell, /const newChatSurface = fullBleedKey === EMPTY_SINGLE_SURFACE_KEY/)
  assert.match(shell, /<NewChatLanding/)
  assert.match(shell, /onRetry=\{requestEmptySingleNewChat\}/)
  // Seamless swap: the landing reuses ChatView's exact empty treatment.
  assert.match(newChatLanding, /className="chat chat--empty"/)
  assert.match(newChatLanding, /className="chat__empty-wrap"/)
  assert.match(newChatLanding, /className="chat__empty-glyph"/)
  assert.match(newChatLanding, /What&apos;s on your mind\?/)
  assert.match(newChatLanding, /Couldn’t start a new chat/)
})

// ── N1: retired v2 plumbing is gone ───────────────────────────────────────────
test('N1: dead exit-presentation plumbing is removed', () => {
  const controller = readFileSync(new URL('../useModeController.js', import.meta.url), 'utf8')
  // The ignored focusedPaneId drag-arm payload is gone (the reducer never read it).
  assert.doesNotMatch(controller, /dragArm = useCallback\(\(focusedPaneId\)/)
  assert.doesNotMatch(controller, /drag-arm', focusedPaneId/)
  assert.match(shell, /mode\.dragArm\(\)/)
  // Polish item 2/3: --ease-mode-chrome + --ease-mode-promote are (re)introduced as
  // USED tokens — the chrome fades + strip-clear ride the chrome curve, the promote
  // FLIP rides the promote curve — so they are no longer the dead plumbing this
  // originally removed.
  assert.match(css, /--ease-mode-chrome: cubic-bezier/)
  assert.match(css, /--ease-mode-promote: cubic-bezier/)
  assert.match(css, /shell-mode-chrome-out 90ms var\(--ease-mode-chrome\)/)
  assert.match(css, /shell-mode-chrome-in 70ms var\(--ease-mode-chrome\)[\s\S]*?calc\(var\(--mode-total, 240ms\) - 70ms\) both/)
  assert.match(css, /shell-mode-strip-clear 100ms var\(--ease-mode-chrome\)/)
  assert.match(css, /shell-mode-promote\s*\n?\s*var\(--mode-duration\)\s*\n?\s*var\(--ease-mode-promote\)/)
  // The unused excludeChatId param is gone; the helper is now the New Chat request
  // (round 4 item 3 — the old freshest-chat write is fully retired).
  assert.doesNotMatch(shell, /excludeChatId/)
  assert.doesNotMatch(shell, /resolveEmptySingleHome/)
  assert.match(shell, /const requestEmptySingleNewChat = useCallback\(\(\) =>/)
  // The stale "Settings conversion" comment near the toggle handler is corrected.
  assert.doesNotMatch(shell, /Settings overlay<->tab conversion/)
})
