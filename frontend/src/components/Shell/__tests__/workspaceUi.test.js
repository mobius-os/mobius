import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const css = readFileSync(
  new URL('../workspace.css', import.meta.url),
  'utf8',
)
const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
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
  // Builder mode forces the single-pane strip visible (the builder surface); a
  // fresh single-screen home still shows nothing until 2+ tabs engage it.
  assert.match(shell, /const tabStripVisible = \(tabStripEngaged \|\| builderModeActive\) && openTabs\.length >= 1/)
  assert.match(shell, /tabStripEngaged[\s\S]*?paneModel\.flattenRollbackPriority\(workspace\)[\s\S]*?: \[\]/)
  // The sole-tab "unpin" shortcut, EXCEPT for a sole Settings tab which must
  // genuinely close (review §11).
  assert.match(shell, /if \(openTabs\.length === 1 && kind !== 'settings'\) \{[\s\S]*?setTabStripEngaged\(false\)[\s\S]*?tabModel\.writeOpenTabs\(\[\]\)/)
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

test('the walkthrough inserts a flag-gated workspace step with pointer-specific copy', () => {
  assert.match(walkthrough, /insertWorkspaceStep\(\s*\[[^\]]*'customize'[^\]]*\], WORKSPACE_SPLITS_ENABLED/)
  assert.match(walkthrough, /step === 'workspace'/)
  assert.match(walkthrough, /Drop it in the middle to keep it as a tab/)
  assert.match(walkthrough, /drop it at the top or bottom to split the screen/)
  // The reduced-motion static mock exists alongside the animated one.
  assert.match(walkthrough, /wt__ws-mock-static/)
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
  assert.match(css, /\.workspace--resizing \.shell__view--paned[\s\S]*?transition: none/)
  assert.match(shell, /el\.classList\.add\('workspace--resizing'\)/)
  assert.match(chrome, /contentEl\.classList\.add\('workspace--resizing'\)/)
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
  assert.match(shell, /className=\{`shell__brand/)
  assert.match(shell, /aria-expanded=\{navigationOpen\}/)
  const onClick = shell.match(/onClick=\{\(e\) => \{[\s\S]*?\n {10}\}\}/)?.[0] || ''
  assert.match(onClick, /if \(logoGesture\.consumeSuppressedClick\(e\.detail\)\) return/)
  assert.match(onClick, /drawerOpen \? closeDrawer\(\) : openDrawer\(\)/)
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
  assert.match(logoGestureSrc, /decidePointerMove\(dx, dy\)/)
  assert.match(logoGestureSrc, /decision === 'swipe'/)
  assert.match(logoGestureSrc, /onToggleMode\?\.\(\)/)
  assert.match(logoGestureSrc, /endPress\(\{ suppressClick: true \}\)/)
  // Suppresses the long-press context menu during a hold.
  assert.match(logoGestureSrc, /if \(pressRef\.current\) e\.preventDefault\(\)/)
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
  assert.match(logoGestureSrc, /if \(isSwipeRight\(dx, dy\)\) \{ onToggleMode\?\.\(\); endPress\(\{ suppressClick: true \}\); return \}/)
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

test('Shell wires the toggle handler, brand ref, and Shift+Enter (no drag-deny vibrate)', () => {
  const handler = shell.match(/const handleToggleViewMode = useCallback\(\(\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  assert.match(handler, /convertSettingsForModeTransition\(\)/)
  assert.match(handler, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE', mode: 'toggle' \}\)/)
  assert.doesNotMatch(handler, /openDrawer|closeDrawer/)
  // The gesture hook receives the toggle + the brand ref (for the ring var). The
  // ref is UNIFIED with the desktop-sidebar focus ref (one ref, both jobs) after
  // the sidebar rebase.
  assert.match(shell, /useLogoModeGesture\(\{[\s\S]*?onToggleMode: handleToggleViewMode/)
  assert.match(shell, /brandRef: brandButtonRef,/)
  // The drag-deny vibrate is DEAD (point 15: dragging is building, never denied).
  assert.doesNotMatch(shell, /viewModeVibrateRef|onDragBlocked/)
  // Keyboard path: Shift+Enter flips the mode (preventDefault keeps it off the drawer).
  assert.match(shell, /e\.shiftKey && e\.key === 'Enter'/)
  assert.match(shell, /brandKeyboardModeClickRef\.current = true/)
  assert.match(shell, /brandKeyboardModeClickRef\.current && e\.detail === 0/)
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
  assert.match(halo, /translate:\s*var\(--halo-x, 0px\) var\(--halo-y, 0px\)/)
  assert.match(halo, /scale:\s*var\(--halo-scale, 1\)/)
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
  assert.match(shell, /<img\s+className="shell__logo"[\s\S]*?draggable=\{false\}[\s\S]*?\/>/)
  // The button keeps its contextmenu suppression during a hold (unchanged).
  assert.match(logoGestureSrc, /if \(pressRef\.current\) e\.preventDefault\(\)/)
})

test('the living halo lifecycle: lit only in builder mode, one allocation-free rAF, paused on hidden, static under reduced motion', () => {
  // Gated on `active` (builder mode) — nothing runs when inactive, and the effect
  // re-runs on active flip so it turns ON at ignite and OFF (cleanup) at snap.
  assert.match(livingHaloSrc, /if \(!el \|\| !active\) return undefined/)
  assert.match(livingHaloSrc, /\}, \[brandRef, active\]\)/)
  // Reduced motion: settle static CSS vars, NO rAF at all.
  assert.match(livingHaloSrc, /if \(prefersReducedMotion\(\)\) \{[\s\S]*?setProperty\('--halo-scale', '1'\)[\s\S]*?return undefined/)
  // One reused frame object → zero per-frame allocation; the drift comes from the
  // pure haloFrame (tested in logoHoldMachine.test.js).
  assert.match(livingHaloSrc, /const frame = \{\} \/\/ reused every tick/)
  assert.match(livingHaloSrc, /haloFrame\(performance\.now\(\), frame\)/)
  // Pauses on a hidden tab (cancel the rAF), resumes on visible.
  assert.match(livingHaloSrc, /document\.visibilityState === 'hidden'/)
  assert.match(livingHaloSrc, /cancelAnimationFrame\(raf\)/)
  // Cleanup kills the loop instantly (the snap) + drops the visibility listener.
  assert.match(livingHaloSrc, /return \(\) => \{[\s\S]*?cancelAnimationFrame\(raf\)[\s\S]*?removeEventListener\('visibilitychange'/)
  // Shell lights the halo only in builder mode, keyed to the SAME brand ref.
  assert.match(shell, /useLivingHalo\(\{ brandRef: brandButtonRef, active: builderModeActive \}\)/)
  assert.match(shell, /<span className="shell__logo-halo" aria-hidden/)
})

test('the room flourish (CHARGE): panes DEAL in on class-apply, suppressed while resizing, instant under reduced motion', () => {
  // The divider-DRAW is gone; the entry flourish is the card-DEAL on the pane wrapper.
  assert.doesNotMatch(css, /workspace-divider-draw/)
  const paned = css.match(/\.shell__view--paned\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(paned, /animation:\s*shell-pane-deal 400ms cubic-bezier\(0\.22, 1, 0\.36, 1\)/)
  assert.match(css, /@keyframes shell-pane-deal\s*\{[\s\S]*?translateX\(18px\)[\s\S]*?translateX\(0\)/)
  // A live resize must not re-deal every frame.
  assert.match(css, /\.workspace--resizing \.shell__view--paned\s*\{\s*\n\s*animation: none;/)
  // Reduced motion drops the deal (and the layout-commit transition).
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(reduced, /\.shell__view--paned \{ transition: none; animation: none; \}/)
})

test('builder single-leaf shows the strip, and entering it has its deal moment (item 3)', () => {
  // The strip is the builder surface: forced visible in builder even at one leaf.
  assert.match(shell, /const tabStripVisible = \(tabStripEngaged \|\| builderModeActive\) && openTabs\.length >= 1/)
  // Entering builder arms a transient beat (single -> panes), reduced-motion skips
  // it, and it batches in the SAME handler as the flip (no un-dealt first frame).
  const handler = shell.match(/const handleToggleViewMode = useCallback\(\(\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  assert.match(handler, /if \(!prefersReducedMotion\(\)\) \{/)
  // Entering (single -> panes) is the else branch of the enter/exit split.
  assert.match(handler, /\} else \{[\s\S]*?setBuilderEntering\(true\)/)
  assert.match(handler, /setTimeout\(\(\) => setBuilderEntering\(false\), BUILDER_ENTER_MS\)/)
  // The transient root class drives the CSS.
  assert.match(shell, /builderEntering \? ' shell--builder-entering' : ''/)
  // CSS: the single-pane strip DEALS in and the single full-bleed pane LIFT-SETTLES.
  assert.match(css, /\.shell--builder-entering \.shell__tabstrip \{[\s\S]*?animation:\s*shell-strip-deal-in 320ms/)
  assert.match(css, /@keyframes shell-strip-deal-in\s*\{[\s\S]*?translateY\(-100%\)[\s\S]*?translateY\(0\)/)
  assert.match(css, /\.shell--builder-entering \.shell__view--active \{[\s\S]*?animation:\s*shell-pane-settle 320ms/)
  assert.match(css, /@keyframes shell-pane-settle\s*\{[\s\S]*?translateY\(8px\) scale\(0\.992\)[\s\S]*?scale\(1\)/)
  // Reduced motion drops the entry deal.
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(reduced, /\.shell--builder-entering \.shell__tabstrip,\s*\n\s*\.shell--builder-entering \.shell__view--active \{ animation: none; \}/)
})

test('leaving builder plays the INVERSE card-deal: deal-out + settle, decisive (item 1)', () => {
  const handler = shell.match(/const handleToggleViewMode = useCallback\(\(\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  // Armed only on a genuine MULTI-PANE exit, never with a focused Settings tab.
  assert.match(handler, /const leavingBuilder = ws\.viewMode !== 'single'/)
  assert.match(handler, /if \(multiPaneRef\.current && !settingsFocused\) \{/)
  assert.match(handler, /setBuilderExiting\(true\)/)
  assert.match(handler, /setTimeout\(\(\) => setBuilderExiting\(false\), BUILDER_EXIT_MS\)/)
  // Faster than the 400ms entry (the Zippo asymmetry survives, deal vocabulary).
  assert.match(shell, /const BUILDER_EXIT_MS = 250/)
  // Held tiled while the beat runs (viewMode already single, effectiveViewMode panes).
  assert.match(shell, /\(\(dragPreviewBuilder \|\| builderExiting\)[\s\S]*?&& workspace\.viewMode === 'single'\)/)
  // The remaining (focused) pane settles to the FULL content box during the beat.
  assert.match(shell, /if \(!builderExiting\) return visibleTabRects/)
  assert.match(shell, /next\.set\(focusedKey, \{ \.\.\.rect, x: 0, y: 0, w: contentRect\.w, h: contentRect\.h \}\)/)
  // Wrappers carry data-pane-role so CSS tells the settling vs leaving pane apart.
  assert.match(shell, /data-pane-role=\{paned[\s\S]*?paned\.paneId === workspace\.focusedPaneId \? 'focused' : 'leaving'/)
  // The transient root class drives the CSS.
  assert.match(shell, /builderExiting \? ' shell--builder-exiting' : ''/)
  // CSS: leaving pane DEALS out to the right + fades; chrome fades out.
  assert.match(css, /\.shell--builder-exiting \.shell__view--paned\[data-pane-role="leaving"\] \{[\s\S]*?animation:\s*shell-pane-deal-out 240ms[\s\S]*?forwards/)
  assert.match(css, /@keyframes shell-pane-deal-out\s*\{[\s\S]*?translateX\(0\)[\s\S]*?translateX\(44px\)[\s\S]*?opacity: 0/)
  assert.match(css, /\.shell--builder-exiting \.workspace__chrome \{[\s\S]*?opacity: 0/)
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
  assert.match(shell, /aria-label="Toggle navigation"/)
  assert.match(shell, /aria-description=\{paneModel\.WORKSPACE_SPLITS_ENABLED\s*\n?\s*\? 'Hold or press Shift\+Enter for builder mode'/)
  assert.match(shell, /role="status" aria-live="polite"/)
  assert.match(shell, /builderModeActive \? 'Builder mode' : 'Single screen'/)
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
  // effectiveViewMode is 'panes' during a single-mode drag preview (point 15).
  assert.match(shell, /viewMode: effectiveViewMode/)
  // effectiveViewMode also holds tiled during the builder EXIT beat (item 1).
  assert.match(shell, /\(dragPreviewBuilder \|\| builderExiting\)[\s\S]*?workspace\.viewMode === 'single'/)
  assert.match(shell, /const \{ multiPane, single, focusedActiveKey, fullBleedKey, visibleAppIds \}/)
  assert.match(shell, /chatPanesVisible && \(!single \|\| paneId === workspace\.focusedPaneId\)/)
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
