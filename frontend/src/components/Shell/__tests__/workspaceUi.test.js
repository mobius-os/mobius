import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const css = readFileSync(
  new URL('../workspace.css', import.meta.url),
  'utf8',
)
const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
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
  assert.match(shell, /const \[tabStripEngaged, setTabStripEngaged\] = useState\(legacyOpenTabs\.length > 0\)/)
  assert.match(shell, /if \(openTabs\.length >= 2\) setTabStripEngaged\(true\)/)
  assert.match(shell, /else if \(openTabs\.length === 0\) setTabStripEngaged\(false\)/)
  assert.match(shell, /const tabStripVisible = tabStripEngaged && openTabs\.length >= 1/)
  assert.match(shell, /tabStripEngaged[\s\S]*?paneModel\.flattenRollbackPriority\(workspace\)[\s\S]*?: \[\]/)
  assert.match(shell, /if \(openTabs\.length === 1\) \{[\s\S]*?setTabStripEngaged\(false\)[\s\S]*?tabModel\.writeOpenTabs\(\[\]\)/)
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

// ── View-mode toggle in the shell top bar (design: view-mode toggle) ────────

const viewModeToggleSrc = readFileSync(new URL('../ViewModeToggle.jsx', import.meta.url), 'utf8')
const shellCss = readFileSync(new URL('../Shell.css', import.meta.url), 'utf8')

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

test('the view-mode toggle is rendered in the shell top bar, in line with the logo, flag-gated', () => {
  // It renders inside .shell__bar (the brand/logo cluster), flag-gated on splits.
  assert.match(shell, /<header className="shell__bar"/)
  assert.match(shell, /\{paneModel\.WORKSPACE_SPLITS_ENABLED && \(\s*<ViewModeToggle/)
  assert.match(shell, /import ViewModeToggle from '\.\/ViewModeToggle\.jsx'/)
  // Right-aligned in the bar via margin-left:auto, quiet by default.
  const rule = shellCss.match(/\.shell__viewmode\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /margin-left:\s*auto/)
})

test('the toggle exposes aria-pressed + a builder/single aria-label and reads as a toggle', () => {
  assert.match(viewModeToggleSrc, /aria-pressed=\{single\}/)
  assert.match(viewModeToggleSrc, /aria-label=\{single \? 'Single screen' : 'Builder mode'\}/)
  // ONE morphing glyph (a single outer frame + a center divider), not a two-SVG swap.
  assert.match(viewModeToggleSrc, /<ViewGlyph \/>/)
  assert.match(viewModeToggleSrc, /className="shell__viewmode-divider"/)
  assert.doesNotMatch(viewModeToggleSrc, /SingleGlyph|PanesGlyph/)
})

test('the toggle activation: instant tap, no-delay double → enter builder, touch swipe', () => {
  // Single tap flips instantly via onToggle; a SECOND activation inside the window
  // is swallowed and idempotently enters builder. There is NO delay on the tap.
  assert.match(viewModeToggleSrc, /const DOUBLE_MS = 300/)
  assert.match(viewModeToggleSrc, /now - lastActivateRef\.current < DOUBLE_MS/)
  assert.match(viewModeToggleSrc, /if \(isDouble\) enterBuilder\(\)\s*\n\s*else onToggle\?\.\(\)/)
  // Touch swipe-right (touch pointers only, dx >= 28, horizontal-dominant) enters builder.
  assert.match(viewModeToggleSrc, /const SWIPE_DX = 28/)
  assert.match(viewModeToggleSrc, /e\.pointerType === 'touch'/)
  assert.match(viewModeToggleSrc, /dx >= SWIPE_DX && Math\.abs\(dx\) > Math\.abs\(dy\)/)
  // enterBuilder calls the prop + replays the morph.
  assert.match(viewModeToggleSrc, /onEnterBuilder\?\.\(\)/)
})

test('the bar toggle never opens or closes the drawer (pure state flip)', () => {
  // The toggle button only flips mode / enters builder — no drawer open/close in
  // the component (prose is fine; a usage is not).
  assert.match(viewModeToggleSrc, /onClick=\{handleClick\}/)
  assert.doesNotMatch(viewModeToggleSrc, /onClose[=(?]/)
  assert.doesNotMatch(viewModeToggleSrc, /openDrawer|closeDrawer/)
  // Shell's toggle handler flips the mode + converts the Settings surface (overlay
  // <-> builder tab); the builder-enter handler is idempotent when already builder.
  const handler = shell.match(/const handleToggleViewMode = useCallback\(\(\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  assert.match(handler, /convertSettingsForModeTransition\(\)/)
  assert.match(handler, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE', mode: 'toggle' \}\)/)
  assert.doesNotMatch(handler, /openDrawer|closeDrawer/)
  const enter = shell.match(/const handleEnterBuilder = useCallback\(\(\) => \{[\s\S]*?\}, \[[^\]]*\]\)/)?.[0] || ''
  assert.match(enter, /viewMode === 'panes'\) return/)
  assert.match(enter, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE', mode: 'panes' \}\)/)
})

test('the toggle is a >=44px target with pan-y pinch-zoom and a CSS-only morph', () => {
  const rule = shellCss.match(/\.shell__viewmode\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /width:\s*44px/)
  assert.match(rule, /height:\s*44px/)
  // Suppress double-tap zoom, keep vertical scroll + pinch (never disable viewport zoom).
  assert.match(rule, /touch-action:\s*pan-y pinch-zoom/)
  // The divider morphs via a ~200ms transition (design's 180-220ms band) + a keyframe.
  const divider = shellCss.match(/\.shell__viewmode-divider\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(divider, /transition:\s*opacity 200ms[^;]*, transform 200ms/)
  assert.match(shellCss, /@keyframes shell-viewmode-morph/)
  assert.match(shellCss, /\.shell__viewmode\[aria-pressed="true"\] \.shell__viewmode-divider/)
  // Reduced motion switches instantly (no transition, no flourish animation).
  assert.match(shellCss, /\.shell__viewmode-divider \{ transition: none; \}/)
  assert.match(shellCss, /\.shell__viewmode\.is-morphing \.shell__viewmode-divider \{ animation: none; \}/)
})

test('Shell threads viewMode into the content derivation and the per-pane chat gate', () => {
  assert.match(shell, /viewMode: workspace\.viewMode/)
  assert.match(shell, /const \{ multiPane, single, focusedActiveKey, fullBleedKey, visibleAppIds \}/)
  assert.match(shell, /chatPanesVisible && \(!single \|\| paneId === workspace\.focusedPaneId\)/)
  // The toggle + vibrate wiring reaches the bar toggle and the drag hook.
  assert.match(shell, /onDragBlocked: \(\) => viewModeVibrateRef\.current\?\.\(\)/)
  assert.match(shell, /onToggle=\{handleToggleViewMode\}/)
  assert.match(shell, /vibrateRef=\{viewModeVibrateRef\}/)
})

test('the drag binding blocks arming + vibrates in single-mode, and folds a split-drop flip into one gesture', () => {
  assert.match(dragBinding, /dragArmingBlocked\(\{ viewMode: wsNow\.viewMode, leafCount: paneIdsInOrder\(wsNow\)\.length \}\)/)
  assert.match(dragBinding, /onDragBlocked\?\.\(\)/)
  // The single-leaf splitting drop folds the flip into OPEN_TAB_AT (one undo step),
  // NOT a following SET_VIEW_MODE.
  assert.match(dragBinding, /flipToPanes = workspaceStateRef\.current\.ws\.viewMode === 'single' && target\.edge != null/)
  assert.match(dragBinding, /flipViewMode: flipToPanes \? 'panes' : null/)
  assert.doesNotMatch(dragBinding, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE'/)
})

test('the attempted-drag vibrate honors prefers-reduced-motion with a non-motion fallback', () => {
  const shake = shellCss.match(/\.shell__viewmode\.is-vibrating\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(shake, /animation:\s*shell-viewmode-shake/)
  assert.match(shellCss, /@keyframes shell-viewmode-shake/)
  // Reduced motion swaps the transform shake for a non-motion outline pulse.
  const reduced = shellCss.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(reduced, /shell-viewmode-pulse/)
  assert.match(reduced, /transform:\s*none/)
  assert.doesNotMatch(
    shellCss.match(/@keyframes shell-viewmode-pulse\s*\{[\s\S]*?\}/)?.[0] || '',
    /transform/,
    'the reduced-motion pulse must not animate transform',
  )
})

test('the active (single) toggle state is a quiet accent tint, not a loud fill', () => {
  const rule = shellCss.match(/\.shell__viewmode\[aria-pressed="true"\]\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /color:\s*var\(--accent\)/)
  assert.match(rule, /background:\s*var\(--accent-dim\)/)
})
