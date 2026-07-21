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
const paneModelSrc = readFileSync(new URL('../paneModel.js', import.meta.url), 'utf8')
const chrome = readFileSync(new URL('../WorkspaceChrome.jsx', import.meta.url), 'utf8')
const dragBinding = readFileSync(new URL('../useWorkspaceDrag.js', import.meta.url), 'utf8')
const paneStrip = readFileSync(new URL('../PaneStrip.jsx', import.meta.url), 'utf8')
const walkthrough = readFileSync(
  new URL('../../Walkthrough/WalkthroughOverlay.jsx', import.meta.url), 'utf8',
)

test('the phone pane switcher keeps a 44px touch target', () => {
  const rule = css.match(/\.workspace__pane-chip\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /min-height:\s*44px/)
})

test('the workspace menu avoids an oversized border-and-shadow card', () => {
  const rule = css.match(/\.workspace__menu\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /border:\s*1px/)
  assert.match(rule, /box-shadow:\s*0 4px 8px/)
  assert.doesNotMatch(rule, /box-shadow:[^;]*(?:1[6-9]|[2-9]\d)px/)
})

test('the workspace menu is labeled, edge-clamped, and arrow-key navigable', () => {
  assert.match(shell, /aria-label="Tab actions"/)
  assert.match(shell, /window\.innerWidth - rect\.width - gutter/)
  assert.match(shell, /e\.key === 'ArrowDown'/)
  assert.match(shell, /querySelector\('\[role="menuitem"\]'\)\?\.focus\(\)/)
  assert.match(shell, /tabMenuReturnFocusRef\.current = e\.currentTarget/)
  assert.match(shell, /returnTarget\?\.focus\?\.\(\{ preventScroll: true \}\)/)
})

test('the compact pane switcher describes its visible pane count', () => {
  assert.match(chrome, /aria-label=\{`Show panes, \$\{projection\.visibleLeaves\.length\} of \$\{allLeaves\.length\} visible`\}/)
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
  // single mode); the engaged latch is the kill-switch world's legacy rule.
  assert.match(shell, /const tabStripVisible = \(SPLITS \? effectiveViewMode === 'panes' : tabStripEngaged\)\s*\n?\s*&& openTabs\.length >= 1/)
  assert.match(shell, /tabStripEngaged[\s\S]*?paneModel\.flattenRollbackPriority\(workspace\)[\s\S]*?: \[\]/)
  // v2 DELETED the legacy sole-tab "unpin" shortcut (deletion list): the sole-tab
  // close is always a real CLOSE_TAB now, so an emptied builder auto-returns to
  // single. The ONE unified close takes a tab object + opts (INV 13).
  assert.doesNotMatch(shell, /openTabs\.length === 1 && kind !== 'settings'/)
  assert.match(shell, /const closeTab = useCallback\(\(tab, \{ reason \} = \{\}\)/)
})

test('the pane switcher uses the shared modal focus and dismissal contract', () => {
  assert.match(chrome, /useDialogFocus\(\{[\s\S]*?open: sheetOpen/)
  assert.match(chrome, /initialFocusRef: sheetCloseRef/)
  assert.match(chrome, /aria-modal="true"/)
  assert.match(chrome, /aria-label="Close pane switcher"/)
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

test('the drag shield owns the grabbing cursor over the whole viewport', () => {
  const rule = css.match(/\.workspace__drag-shield\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /position:\s*fixed/)
  assert.match(rule, /inset:\s*0/)
  assert.match(rule, /cursor:\s*grabbing/)
  // The shield must out-layer the drawer (Drawer.css z-index 90/95) so a
  // left-edge drag hits the drop zone, never the drawer beneath it.
  const z = Number(rule.match(/z-index:\s*(\d+)/)?.[1] || 0)
  assert.ok(z >= 100, `drag shield z-index ${z} must sit above the drawer (95)`)
})

test('reduced motion makes the drop preview instant', () => {
  const block = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(block, /\.workspace__drop-preview\s*\{\s*transition:\s*none/)
})

test('the coachmark carries pointer-specific copy and dismisses without a stray tap', () => {
  assert.match(shell, /workspaceCoachmarkVisible/)
  // M6: gated to the effective builder world + non-immersive, so it never shows in
  // single mode (no tabs) or over an immersive-solo (z-120).
  assert.match(shell, /builderWorld: effectiveViewMode === 'panes' && !immersiveActive/)
  assert.match(shell, /coarsePointer \? 'Hold a tab to move it' : 'Drag tabs to split the view'/)
  assert.match(shell, /onClick=\{dismissWorkspaceCoachmark\}/)
  // 12s auto-dismiss, never an unrelated pointerdown.
  assert.match(shell, /setTimeout\(dismissWorkspaceCoachmark, 12000\)/)
  assert.doesNotMatch(shell, /coachmark[\s\S]{0,80}addEventListener\('pointerdown'/)
  const hintRule = css.match(/\.workspace__coachmark\s*\{[\s\S]*?\}/)?.[0] || ''
  const closeRule = css.match(/\.workspace__coachmark-close\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(hintRule, /pointer-events:\s*none/)
  assert.match(closeRule, /pointer-events:\s*auto/)
})

test('post-drag click suppression is limited to the original source', () => {
  assert.match(dragBinding, /function suppressNextSourceClick\(sourceEl\)/)
  assert.match(dragBinding, /path\.includes\(sourceEl\)/)
  assert.match(dragBinding, /if \(!belongsToSource\) return/)
  assert.match(dragBinding, /suppressNextSourceClick\(srcEl\)/)
})

test('the undo chord is flag-gated and defers to focused inputs', () => {
  assert.match(shell, /if \(!paneModel\.WORKSPACE_SPLITS_ENABLED\) return undefined[\s\S]*?undoKeyPressed\(e\)/)
  assert.match(shell, /isEditableTarget\(document\.activeElement\)/)
  assert.match(shell, /dispatchWorkspace\(\{ type: 'UNDO_LAST' \}\)/)
})

test('the first-run walkthrough stays short and action-first', () => {
  assert.match(walkthrough, /const STEPS = \['intro', 'home', 'first-chat'\]/)
  assert.doesNotMatch(walkthrough, /step === 'workspace'/)
  // The recovery net is named next to the capability it backstops; a future
  // trim of the walkthrough must not silently drop it.
  assert.match(walkthrough, /\/recover runs outside Möbius/)
  assert.match(walkthrough, /Meet my Möbius/)
  assert.match(walkthrough, /mobius:walkthrough-completed/)
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

test('the context menu offers Close pane when another pane can absorb the space', () => {
  assert.match(shell, /type: 'CLOSE_PANE', paneId: tabMenu\.paneId/)
  assert.match(shell, /Close pane/)
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

test('the strips are roving-tabindex toolbars (one shared implementation)', () => {
  assert.match(paneStrip, /export function stripKeyDown/)
  assert.match(paneStrip, /tabIndex=\{active \? 0 : -1\}/)
  assert.match(paneStrip, /e\.key === 'ArrowRight'/)
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

test('the pane chip and sheet rows carry an activity dot for hidden panes', () => {
  assert.match(chrome, /function paneHasActivity/)
  assert.match(chrome, /workspace__pane-chip-dot/)
  assert.match(chrome, /workspace__sheet-row-dot/)
  assert.match(shell, /streamingChatIds=\{streamingChatIds\}/)
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

// ── Builder-mode control lives on the LOGO (owner placement) ────────────────

const logoGestureSrc = readFileSync(new URL('../useLogoModeGesture.js', import.meta.url), 'utf8')
const shellCss = readFileSync(new URL('../Shell.css', import.meta.url), 'utf8')
const livingHaloSrc = readFileSync(new URL('../useLivingHalo.js', import.meta.url), 'utf8')

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

test('there is NO standalone view-mode toggle button — the logo is the control', () => {
  assert.match(shell, /<header className="shell__bar"/)
  // The old top-right toggle component and its class are gone entirely.
  assert.doesNotMatch(shell, /ViewModeToggle/)
  assert.doesNotMatch(shell, /shell__viewmode/)
  assert.doesNotMatch(shellCss, /\.shell__viewmode\b/)
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
  assert.match(logoGestureSrc, /if \(movedBeyondSlop\(dx, dy\)\) \{ endPress\(\{ suppressClick: true \}\); return \}/)
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

test('the logo mark IS the indicator (CHARGE): compress on hold + spring/snap + 180° twist + tint + living halo', () => {
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
  // destination arrival, not the last deal-out. Halo bloom + wordmark tint keep pace.
  assert.match(shellCss, /\.shell--builder-entering \.shell__logo\s*\{[\s\S]*?rotate var\(--mode-total, 260ms\) cubic-bezier\(0\.2, 1, 0\.32, 1\)/)
  assert.match(shellCss, /\.shell--builder-exiting \.shell__logo\s*\{[\s\S]*?rotate var\(--mode-total, 220ms\) cubic-bezier\(0\.25, 0\.8, 0\.25, 1\)/)
  assert.match(shellCss, /\.shell--builder-entering \.shell__logo-halo\s*\{\s*transition: opacity 160ms var\(--ease-mode-arrive\) 60ms/)
  assert.match(shellCss, /\.shell--builder-exiting \.shell__logo-halo\s*\{\s*transition: opacity 100ms var\(--ease-mode-chrome\)/)
  assert.match(shellCss, /\.shell--builder-entering \.shell__wordmark \{ transition-duration: 220ms; \}/)
  assert.match(shellCss, /\.shell--builder-exiting \.shell__wordmark \{ transition-duration: 140ms; \}/)
  // The LIVING HALO: a radial-gradient element behind the mark, driven by the rAF
  // vars, lit only in builder mode, per-theme base alpha via --halo-alpha.
  // Anchor to the BASE rule (newline-prefixed), not the beat-scoped
  // `.shell--builder-* .shell__logo-halo` overrides added by polish item 5.
  const halo = shellCss.match(/\n\.shell__logo-halo\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(halo, /radial-gradient/)
  assert.match(halo, /var\(--halo-alpha, 0\.5\)/)
  assert.match(halo, /translate:\s*0 0/)
  assert.match(halo, /scale:\s*1/)
  assert.match(shellCss, /\.shell__brand--builder \.shell__logo-halo\s*\{[\s\S]*?opacity:\s*var\(--halo-opacity, 0\.85\)/)
  // Per-theme alpha token: keyed off the APP theme (data-theme), NOT the OS
  // prefers-color-scheme (V2) — a dark app theme under a light OS gets the dark
  // value. Dark is the default (base .shell), light is the explicit override.
  assert.match(shellCss, /\.shell \{ --halo-alpha: 0\.4; \}/)
  assert.match(shellCss, /:root\[data-theme="light"\] \.shell \{ --halo-alpha: 0\.5; \}/)
  assert.doesNotMatch(shellCss, /@media \(prefers-color-scheme: dark\)[\s\S]*?--halo-alpha/)
  // Reduced motion: twist instant, the compress kept (direct press feedback), the
  // spring/snap skipped (haptic still fires in JS), halo static (no rAF).
  assert.match(shellCss, /\.shell__logo \{ transition: rotate 0s, scale 160ms ease; \}/)
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

test('the living halo lifecycle: lit only in builder mode, one allocation-free rAF, paused on hidden, static under reduced motion', () => {
  // Gated on `active` (builder mode) — nothing runs when inactive, and the effect
  // re-runs on active flip so it turns ON at ignite and OFF (cleanup) at snap.
  assert.match(livingHaloSrc, /if \(!el \|\| !active\) return undefined/)
  // The effect re-runs on active flip AND on a reduced-motion preference change
  // (finding 13): the halo subscribes to the media query so enabling reduce
  // mid-session settles the static halo instead of leaving the rAF running.
  assert.match(livingHaloSrc, /\}, \[haloRef, active, reduced\]\)/)
  assert.match(livingHaloSrc, /matchMedia\('\(prefers-reduced-motion: reduce\)'\)/)
  assert.match(livingHaloSrc, /mq\.addEventListener\?\.\('change', onChange\)/)
  // Reduced motion: settle static CSS vars, NO rAF at all.
  assert.match(livingHaloSrc, /if \(reduced\) \{[\s\S]*?el\.style\.scale = '1'[\s\S]*?clearHaloStyles\(el\)/)
  // One reused frame object → zero per-frame allocation; the drift comes from the
  // pure haloFrame (tested in logoHoldMachine.test.js).
  assert.match(livingHaloSrc, /const frame = \{\} \/\/ reused every tick/)
  assert.match(livingHaloSrc, /haloFrame\(performance\.now\(\), frame\)/)
  // Pauses on a hidden tab (cancel the rAF), resumes on visible.
  assert.match(livingHaloSrc, /document\.visibilityState === 'hidden'/)
  assert.match(livingHaloSrc, /cancelAnimationFrame\(raf\)/)
  // Cleanup kills the loop instantly (the snap) + drops the visibility listener.
  assert.match(livingHaloSrc, /return \(\) => \{[\s\S]*?cancelAnimationFrame\(raf\)[\s\S]*?removeEventListener\('visibilitychange'/)
  // The isolated brand lights a leaf ref only in builder mode AND when no beat is
  // live (haloActive = builderModeActive && !modeState.transition), so the halo's
  // rAF never competes with the deal animation (exit-design v2 §Background isolation).
  assert.match(shellBrand, /useLivingHalo\(\{ haloRef, active: splitsEnabled && haloActive \}\)/)
  assert.match(shell, /haloActive=\{builderModeActive && !modeState\.transition\}/)
  assert.match(shellBrand, /<span ref=\{haloRef\} className="shell__logo-halo" aria-hidden/)
  assert.match(livingHaloSrc, /clearHaloStyles\(el\)/)
})

test('entry deals in on the keyed beat class, compositor-only, instant under reduced motion (v2)', () => {
  // v2: entry is a transform/opacity DEAL-IN keyed to the transient
  // .shell--builder-entering class, applied per-wrapper via data-mode-motion (never
  // the permanent .shell__view--paned). No animated layout property, shadow, or radius.
  const panedBase = css.match(/\.shell__view--paned \{[\s\S]*?\n\}/)?.[0] || ''
  assert.doesNotMatch(panedBase, /animation:/)
  assert.doesNotMatch(panedBase, /transition:/)
  assert.match(css, /\.shell--builder-entering\s*\n\.shell__view\[data-mode-motion="deal-in"\] \{[\s\S]*?animation:\s*\n?\s*shell-mode-deal-in/)
  // The deal-in keyframe touches ONLY transform + opacity (no shadow/radius/filter).
  const dealIn = css.match(/@keyframes shell-mode-deal-in\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(dealIn, /translate3d\(28px, -6px, 0\) scale\(0\.982\)/)
  assert.match(dealIn, /opacity: 0/)
  assert.doesNotMatch(dealIn, /box-shadow|border-radius|filter|clip/)
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
  // at one leaf, and never in single mode.
  assert.match(shell, /const tabStripVisible = \(SPLITS \? effectiveViewMode === 'panes' : tabStripEngaged\)\s*\n?\s*&& openTabs\.length >= 1/)
  const handler = shell.match(/const handleToggleViewMode = useCallback\(\(cause\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  // v2: the handler builds the latched plan (deriveEnter/ExitPlan from the
  // projection) and dispatches the controller beat + the durable flip in the SAME
  // handler (INV 2/3). No Shell timer, no per-pane role plumbing.
  // The plan derives from the SETTLED post-flip state (the synchronous reducer
  // preview): the durable flip and the null-slot home resolution land first, so
  // the beat animates toward the surface single mode will actually paint.
  assert.match(handler, /deriveEnterPlan\(\{ workspace: settled, projection \}\)/)
  assert.match(handler, /mode\.toggle\(\{ cause, presentation \}\)/)
  assert.match(handler, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE', mode: 'toggle' \}\)/)
  assert.match(shell, /modeMachine\.transitionRootClass\(modeState/)
  // The single-pane nav strip carries the beat motion so it deals WITH its pane.
  assert.match(shell, /data-mode-motion=\{navMotion \? navMotion\.motion : undefined\}/)
  // M4: and it is INERT throughout the exit beat (not just under the drawer), so a
  // tap on the strip while it clears cannot re-target the transition.
  assert.match(shell, /className="shell__tabstrip"[\s\S]*?inert=\{modalDrawerOpen \|\| exitBeatActive\}/)
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
  // reveal the single world underneath), the 20ms stagger, and the FLIP rects — the
  // handler no longer computes settlePaneId / leavingPaneIds / dealMultiPane itself.
  assert.match(handler, /const leavingBuilder = ws\.viewMode !== 'single'/)
  assert.match(handler, /deriveExitPlan\(\{[\s\S]*?workspace: settled, projection, contentRect,/)
  // A flip to single REQUESTS the New Chat landing for a null/never-seeded slot BEFORE
  // the plan derives (round 4 item 3): the slot stays null through the beat so
  // exitTargetKey reveals home:new-chat, and the row materializes after the descriptor
  // idles — never the freshest chat, never a blank home.
  assert.match(handler, /if \(leavingBuilder\) requestEmptySingleNewChat\(\)/)
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
  // CSS v2: promote FLIPs to full-bleed; deal-out drifts + fades. Both compositor-only.
  assert.match(css, /\.shell--builder-exiting\s*\n\.shell__view\[data-mode-motion="promote"\] \{[\s\S]*?shell-mode-promote/)
  assert.match(css, /\.shell--builder-exiting\s*\n\.shell__view\[data-mode-motion="deal-out"\] \{[\s\S]*?shell-mode-deal-out/)
  const dealOut = css.match(/@keyframes shell-mode-deal-out\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(dealOut, /translate3d\(28px, -6px, 0\) scale\(0\.982\)/)
  assert.match(dealOut, /opacity: 0/)
  assert.doesNotMatch(dealOut, /box-shadow|border-radius|filter|clip/)
  // The parent-chrome opacity fade is DELETED (strips deal with their panes now).
  assert.doesNotMatch(css, /\.shell--builder-exiting \.workspace__chrome \{[\s\S]*?opacity: 0/)
  // Reduced motion drops any beat.
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(reduced, /\.shell \[data-mode-motion\] \{ animation: none !important; \}/)
})

test('round 4 item 2: the world-reveal underlay is a GATING destination arrival, not a breathe', () => {
  // Phase 2: the destination settles in (opacity .60→1, scale 1.012→1) over
  // --mode-arrive-duration delayed by --mode-arrive-delay (Shell writes both from the
  // plan's destinationMotion). `both` holds the veil through the delay.
  assert.match(css, /\.shell--builder-exiting \.shell__view--exit-underlay \{[\s\S]*?animation:\s*\n?\s*shell-mode-destination-arrive[\s\S]*?var\(--mode-arrive-duration\)[\s\S]*?var\(--mode-arrive-delay\)[\s\S]*?both/)
  const arrive = css.match(/@keyframes shell-mode-destination-arrive\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(arrive, /from \{ transform: scale\(1\.012\); opacity: 0\.60; \}/)
  assert.match(arrive, /to \{ transform: scale\(1\); opacity: 1; \}/)
  // Compositor-only: transform + opacity, nothing that forces layout.
  assert.doesNotMatch(arrive, /box-shadow|border-radius|filter|clip|top:|left:|width:|height:/)
  // The old atmospheric breathe is fully retired.
  assert.doesNotMatch(css, /shell-mode-underlay-settle/)
  const workspaceView = readFileSync(new URL('../workspaceView.js', import.meta.url), 'utf8')
  assert.doesNotMatch(workspaceView, /underlay-settle/)
  // GATING: workspaceView DOES name it now (its completionNames add destination-arrive
  // for a world reveal), so the descriptor awaits the delayed arrival.
  assert.match(workspaceView, /DESTINATION_ARRIVE_NAME = 'shell-mode-destination-arrive'/)
  // Defensive reduced-motion rule: drop the arrival under reduce.
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(reduced, /\.shell--builder-exiting \.shell__view--exit-underlay \{ animation: none; \}/)
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

test('the mobile modal keeps its existing brand close path while the workspace is inert', () => {
  const header = shell.match(/<header className="shell__bar"[^>]*>/)?.[0] || ''
  assert.doesNotMatch(header, /inert=/)
  assert.match(shell, /<main className="shell__content" inert=\{modalDrawerOpen\}/)
  assert.match(shellBrand, /aria-expanded=\{navigationOpen\}/)
  assert.match(shell, /drawerOpen \? closeDrawer\(\) : openDrawer\(\)/)
})

test('large drawer lists memoize ordering and row actions without changing row ownership', () => {
  assert.match(drawer, /const allChats = useMemo\(/)
  assert.match(drawer, /const sortedApps = useMemo\(/)
  assert.match(drawer, /const rowActions = useMemo\(/)
  assert.match(drawer, /const DrawerRow = memo\(function DrawerRow/)
  assert.match(drawer, /item=\{chat\}[\s\S]*?actions=\{rowActions\}/)
  assert.match(drawer, /item=\{app\}[\s\S]*?actions=\{rowActions\}/)
  assert.doesNotMatch(drawer, /onSelect=\{\(\) => on(?:Chat|App)/)
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
  // The walkthrough is a dismissible dialog (skip + onClose:skip) — reloading it
  // over a builder workspace can never trap; and the update-review modal stays fixed.
  assert.match(walkthrough, /const skip = useCallback/)
  assert.match(walkthrough, /onClose: skip/)
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
  // Chat PAINTING is gated on the two-worlds painting set (single mode paints only
  // the slot chat; builder paints each visible pane's chat), separate from MOUNTING.
  assert.match(shell, /visible=\{chatPanesVisible && role !== 'held' && visibleChatKeys\.has\(`chat:\$\{chatId\}`\)\}/)
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
  assert.match(dragBinding, /const onPageHide = \(\) => cleanup\(\{ suppressClick: armed \}\)/)
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
  //     pointer is dead (different pointerId + no live capture), then proceeds. A live
  //     drag keeps its capture, so this never cancels one.
  assert.match(dragBinding, /function standingSessionPointerIsLive\(\) \{[\s\S]*?hasPointerCapture\?\.\(activePointerId\)/)
  assert.match(dragBinding, /if \(e\.pointerId !== activePointerId && !standingSessionPointerIsLive\(\)\) \{[\s\S]*?activeCleanup\(\{ suppressClick: true \}\)/)
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

test('visual nits V3-V6: coachmark clears the strip, focus underline reads, chip clamps, cancel blurs', () => {
  // V3: the first-use coachmark anchors BELOW the strip (bar 58px + safe-area + strip
  // 34px + 8px gap), so its pill never covers a tab label.
  const coach = css.match(/\.workspace__coachmark \{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(coach, /top: calc\(58px \+ env\(safe-area-inset-top\) \+ 42px\)/)
  assert.doesNotMatch(coach, /top: 60px/)
  // V4: the FOCUSED pane's active pill softens the base full-accent border so the 2px
  // underline is what carries focus (the border used to mask it).
  const focused = css.match(/\.workspace__strip--focused \.shell__tab--active \{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(focused, /box-shadow: inset 0 -2px 0 0 var\(--accent\)/)
  assert.match(focused, /border-color: color-mix\(in srgb, var\(--accent\) 45%, var\(--border-light\)\)/)
  // V5: the drag chip clamps within the viewport so its label never clips at the
  // right edge (measured offsetWidth + an 8px margin).
  assert.match(dragBinding, /const maxLeft = Math\.max\(margin, window\.innerWidth - w - margin\)/)
  assert.match(dragBinding, /Math\.max\(margin, Math\.min\(left, maxLeft\)\)/)
  // V6: a CANCELLED drag blurs the drag-origin row so its focus ring clears; a
  // committed drop keeps focus (the tab moved).
  assert.match(dragBinding, /if \(suppressClick && !committed\) srcEl\.blur\?\.\(\)/)
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
  // Close as deleted (reducer clears the slot), then land on the New Chat surface
  // instead of a blank single screen (round 4 item 3).
  assert.match(effect, /reason: 'deleted'/)
  assert.match(effect, /requestEmptySingleNewChat\(\)/)
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
  // Stale-guard: token still current, still single, slot still null, no live beat.
  assert.match(fn, /newChatRequestSeqRef\.current !== pending\.token/)
  assert.match(fn, /!single \|\| ws\.singleScreen != null \|\| modeTransitionRef\.current/)
  // offline/failed → keep the landing with a retry state, never chats[0].
  assert.match(fn, /if \(chatId == null\) \{[\s\S]*?setNewChatLandingOffline\(true\)/)
  // The slot write is history-free (applyModeDestination pushes none) + preserveSettings,
  // and there is NO composer focus (a mode toggle must not summon the keyboard).
  assert.match(fn, /applyModeDestination\(\s*\{ view: 'chat', chatId, appId: null, paneId: ws\.focusedPaneId \},\s*\{ preserveSettings: true \}/)
  assert.doesNotMatch(fn, /requestComposerFocus|focusComposer/)
})

test('round4-3: resolveNewChatId is the shared reuse-and-create policy; newChat + materialize both use it', () => {
  assert.match(shell, /async function resolveNewChatId\(\{ candidate, draft, forceNew, exclude \} = \{\}\)/)
  // newChat consumes the shared resolver, not its own inline reuse/create.
  assert.match(shell, /const \{ chatId, reason \} = await resolveNewChatId\(\{ draft, forceNew, exclude \}\)/)
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
  assert.match(newChatLanding, /What&apos;s on your mind\?/)
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
  assert.match(css, /shell-mode-chrome-in 110ms var\(--ease-mode-chrome\) 84ms/)
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
