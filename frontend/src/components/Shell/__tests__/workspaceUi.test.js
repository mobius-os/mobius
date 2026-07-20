import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const css = readFileSync(
  new URL('../workspace.css', import.meta.url),
  'utf8',
)
const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
const shellBrand = readFileSync(new URL('../ShellBrand.jsx', import.meta.url), 'utf8')
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
  // The sole-tab "unpin" shortcut is KILL-SWITCH-world only (with splits ON
  // the sole-tab close is a real CLOSE_TAB so auto-return can fire), and even
  // there a sole Settings tab must genuinely close (review §11).
  assert.match(shell, /if \(!SPLITS && openTabs\.length === 1 && kind !== 'settings'\) \{[\s\S]*?setTabStripEngaged\(false\)[\s\S]*?tabModel\.writeOpenTabs\(\[\]\)/)
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
  // First-appear fade (80ms) + zone-to-zone morph (120ms cubic-bezier).
  assert.match(rule, /opacity 80ms/)
  assert.match(rule, /120ms cubic-bezier\(0\.2, 0, 0, 1\)/)
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

test('a layout commit blooms the paned wrappers, suppressed while resizing', () => {
  const rule = css.match(/\.shell__view--paned\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /transition:\s*top 180ms ease-out/)
  assert.match(css, /\.workspace--container-resizing \.shell__view--paned[\s\S]*?transition: none/)
  assert.match(css, /\.workspace--divider-dragging \.shell__view--paned[\s\S]*?transition: none/)
  assert.match(shell, /el\.classList\.add\('workspace--container-resizing'\)/)
  assert.match(shell, /el\.classList\.remove\('workspace--container-resizing'\)/)
  assert.match(chrome, /contentEl\.classList\.add\('workspace--divider-dragging'\)/)
  assert.doesNotMatch(shell, /workspace--divider-dragging/,
    'the ResizeObserver must not own the divider guard')
  assert.doesNotMatch(chrome, /workspace--container-resizing/,
    'the divider drag must not own the ResizeObserver guard')
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
  assert.match(handler, /convertSettingsForModeTransition\(\)/)
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
  // Completion: spring (enter) overshoots 0.84→1 on a springy cubic; snap (exit) settles fast.
  assert.match(shellCss, /\.shell__brand\.is-igniting \.shell__logo\s*\{[\s\S]*?animation:\s*shell-logo-ignite 480ms cubic-bezier\(0\.22, 1\.6, 0\.36, 1\)/)
  assert.match(shellCss, /\.shell__brand\.is-snapping \.shell__logo\s*\{[\s\S]*?animation:\s*shell-logo-snap 150ms ease/)
  assert.match(shellCss, /@keyframes shell-logo-ignite\s*\{[\s\S]*?scale:\s*0\.84[\s\S]*?scale:\s*1/)
  assert.match(shellCss, /@keyframes shell-logo-snap\s*\{[\s\S]*?scale:\s*0\.84/)
  // The LIVING HALO: a radial-gradient element behind the mark, driven by the rAF
  // vars, lit only in builder mode, per-theme base alpha via --halo-alpha.
  const halo = shellCss.match(/\.shell__logo-halo\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(halo, /radial-gradient/)
  assert.match(halo, /var\(--halo-alpha, 0\.5\)/)
  assert.match(halo, /translate:\s*0 0/)
  assert.match(halo, /scale:\s*1/)
  assert.match(shellCss, /\.shell__brand--builder \.shell__logo-halo\s*\{[\s\S]*?opacity:\s*var\(--halo-opacity, 0\.85\)/)
  // Per-theme alpha token: quieter in dark.
  assert.match(shellCss, /\.shell \{ --halo-alpha: 0\.5; \}/)
  assert.match(shellCss, /@media \(prefers-color-scheme: dark\)\s*\{[\s\S]*?--halo-alpha: 0\.4/)
  // Reduced motion: twist instant, the compress kept (direct press feedback), the
  // spring/snap skipped (haptic still fires in JS), halo static (no rAF).
  assert.match(shellCss, /\.shell__logo \{ transition: rotate 0s, scale 160ms ease; \}/)
  assert.match(shellCss, /\.shell__brand\.is-igniting \.shell__logo,\s*\n\s*\.shell__brand\.is-snapping \.shell__logo \{ animation: none; \}/)
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
  // The isolated brand lights a leaf ref only in builder mode and cleans inline styles.
  assert.match(shellBrand, /useLivingHalo\(\{ haloRef, active: splitsEnabled && builderModeActive \}\)/)
  assert.match(shellBrand, /<span ref=\{haloRef\} className="shell__logo-halo" aria-hidden/)
  assert.match(livingHaloSrc, /clearHaloStyles\(el\)/)
})

test('the room flourish (CHARGE): panes DEAL in on the KEYED beat class, suppressed while resizing, instant under reduced motion', () => {
  // The divider-DRAW is gone; the entry flourish is the card-DEAL on the pane wrapper.
  assert.doesNotMatch(css, /workspace-divider-draw/)
  // INV 11: the deal is owned by the transient .shell--builder-entering beat
  // class, NOT the permanent .shell__view--paned (which used to replay it on every
  // resize). The base paned block carries the geometry transition but no animation.
  const panedBase = css.match(/\.shell__view--paned \{[\s\S]*?\n\}/)?.[0] || ''
  assert.doesNotMatch(panedBase, /animation:\s*shell-pane-deal/)
  assert.match(css, /\.shell--builder-entering \.shell__view--paned \{[\s\S]*?animation:\s*shell-pane-deal 400ms cubic-bezier\(0\.22, 1, 0\.36, 1\)/)
  assert.match(css, /@keyframes shell-pane-deal\s*\{[\s\S]*?translateX\(18px\)[\s\S]*?translateX\(0\)/)
  // A live resize / divider-drag during the enter beat must not re-deal every frame
  // (finding F14): the REAL operation classes on .shell__content, in the REAL DOM
  // order (root .shell--builder-entering → content op-class → pane). The old
  // `.workspace--resizing` selector matched nothing and had an inverted ancestor order.
  assert.match(css, /\.shell--builder-entering \.workspace--container-resizing \.shell__view--paned,\s*\n\s*\.shell--builder-entering \.workspace--divider-dragging \.shell__view--paned \{ animation: none; \}/)
  assert.doesNotMatch(css, /\.workspace--resizing \.shell--builder-entering/)
  // Reduced motion drops the deal (and the layout-commit transition).
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(reduced, /\.shell__view--paned \{ transition: none; animation: none; \}/)
})

test('builder single-leaf shows the strip, and entering it has its deal moment (item 3)', () => {
  // The strip is the builder surface: visible in the effective builder world even
  // at one leaf, and never in single mode.
  assert.match(shell, /const tabStripVisible = \(SPLITS \? effectiveViewMode === 'panes' : tabStripEngaged\)\s*\n?\s*&& openTabs\.length >= 1/)
  // Entering builder routes through the ONE mode controller (INV 2), batched in
  // the SAME handler as the durable flip (INV 7) so no un-dealt frame paints. The
  // beat + reduced-motion collapse live in the machine now, not a Shell timer.
  const handler = shell.match(/const handleToggleViewMode = useCallback\(\(cause\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  // The honest cause threads through into the descriptor (finding F13).
  assert.match(handler, /mode\.toggle\(\{ cause, focusedPaneId, leavingPaneIds, multiPane: dealMultiPane \}\)/)
  assert.match(handler, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE', mode: 'toggle' \}\)/)
  // The transient root class comes from the descriptor (exactly one beat class).
  assert.match(shell, /modeMachine\.transitionRootClass\(modeState/)
  // CSS: the single-pane strip DEALS in and the single full-bleed pane LIFT-SETTLES;
  // the multi-pane enter deal is now keyed to the transient class (INV 11), never
  // the permanent .shell__view--paned.
  assert.match(css, /\.shell--builder-entering \.shell__tabstrip \{[\s\S]*?animation:\s*shell-strip-deal-in 320ms/)
  assert.match(css, /@keyframes shell-strip-deal-in\s*\{[\s\S]*?translateY\(-100%\)[\s\S]*?translateY\(0\)/)
  assert.match(css, /\.shell--builder-entering \.shell__view--active \{[\s\S]*?animation:\s*shell-pane-settle 320ms/)
  assert.match(css, /\.shell--builder-entering \.shell__view--paned \{[\s\S]*?animation:\s*shell-pane-deal 400ms/)
  assert.match(css, /@keyframes shell-pane-settle\s*\{[\s\S]*?translateY\(8px\) scale\(0\.992\)[\s\S]*?scale\(1\)/)
  // INV 11 (P1 fix): the PERMANENT .shell__view--paned carries NO one-shot deal —
  // it used to replay the 400ms entrance after every resize / dock / divider drag.
  const panedBase = css.match(/\.shell__view--paned \{[\s\S]*?\n\}/)?.[0] || ''
  assert.doesNotMatch(panedBase, /animation:\s*shell-pane-deal/)
  // Reduced motion drops the entry deal.
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(reduced, /\.shell--builder-entering \.shell__tabstrip,\s*\n\s*\.shell--builder-entering \.shell__view--active \{ animation: none; \}/)
})

test('leaving builder plays the INVERSE card-deal: deal-out + settle, decisive (item 1)', () => {
  const handler = shell.match(/const handleToggleViewMode = useCallback\(\(cause\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  // The exit deal is earned only for a genuine MULTI-PANE exit with a non-Settings
  // focused surface; multiPane=false makes the machine collapse instantly (the
  // beat + duration + Zippo asymmetry live in modeMachine.js: MODE_EXIT_MS < MODE_ENTER_MS).
  assert.match(handler, /const leavingBuilder = ws\.viewMode !== 'single'/)
  assert.match(handler, /multiPaneRef\.current && !settingsFocused/)
  // The leaving panes are LATCHED into the descriptor (INV 9): a mid-beat focus
  // change cannot re-target which pane deals out. The SETTLING pane is the slot's
  // pane (animation honesty), not necessarily the focused one.
  assert.match(handler, /const leavingPaneIds = leavingBuilder/)
  assert.match(handler, /visibleLeavesRef\.current\.filter\(id => id !== settlePaneId\)/)
  assert.match(handler, /const settlePaneId = \(slotPane && visibleLeavesRef\.current\.includes\(slotPane\.id\)\)/)
  // Held tiled while the beat runs — from the ONE descriptor (INV 4), not a boolean pair.
  assert.match(shell, /const effectiveViewMode = modeMachine\.effectiveViewMode\(modeState/)
  // The settling pane grows to the FULL content box during the beat.
  assert.match(shell, /if \(!exitGeometryActive\) return visibleTabRects/)
  assert.match(shell, /next\.set\(settleKey, \{ \.\.\.rect, x: 0, y: 0, w: contentRect\.w, h: contentRect\.h \}\)/)
  // Wrappers carry the LATCHED data-pane-role so CSS tells settling from leaving.
  assert.match(shell, /data-pane-role=\{paneRoleFor\(/)
  // The transient root class comes from the descriptor (exactly one beat class, INV 1).
  assert.match(shell, /modeMachine\.transitionRootClass\(modeState/)
  // CSS: leaving pane DEALS out to the right + fades; chrome fades AND — INV 9 —
  // the leaving surfaces + chrome children go POINTER-INERT (not merely invisible).
  assert.match(css, /\.shell--builder-exiting \.shell__view--paned\[data-pane-role="leaving"\] \{[\s\S]*?animation:\s*shell-pane-deal-out 240ms[\s\S]*?forwards/)
  assert.match(css, /@keyframes shell-pane-deal-out\s*\{[\s\S]*?translateX\(0\)[\s\S]*?translateX\(44px\)[\s\S]*?opacity: 0/)
  assert.match(css, /\.shell--builder-exiting \.workspace__chrome \{[\s\S]*?opacity: 0/)
  assert.match(css, /\.shell--builder-exiting[\s\S]*?\.workspace__strip,[\s\S]*?pointer-events: none/)
  // Reduced motion drops the exit deal.
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(reduced, /\.shell--builder-exiting \.shell__view--paned\[data-pane-role="leaving"\] \{ animation: none; \}/)
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
  // BLOCKER §1: the drag-commit flip routes through the SAME centralized conversion.
  assert.match(dragBinding, /if \(flipToPanes\) \{[\s\S]*?convertSettingsForModeTransition\?\.\(\)/)
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
