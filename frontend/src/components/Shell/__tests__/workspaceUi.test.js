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

// ── View-mode toggle (design: view-mode toggle) ─────────────────────────────

const drawer = readFileSync(new URL('../../Drawer/Drawer.jsx', import.meta.url), 'utf8')
const drawerCss = readFileSync(new URL('../../Drawer/Drawer.css', import.meta.url), 'utf8')
// The ViewModeToggle component body (last function in the file) — sliced so the
// "no onClose" contract is asserted against the toggle, not the whole drawer.
const viewModeToggleSrc = drawer.match(/function ViewModeToggle\([\s\S]*$/)?.[0] || ''

test('the view-mode toggle is placed inline on the drawer Settings footer row, flag-gated', () => {
  // It renders inside the bottom (Settings) group, gated on the splits flag, as a
  // SIBLING of the Settings button (never nested — nested <button>s are invalid).
  assert.match(drawer, /drawer__group drawer__group--bottom/)
  assert.match(drawer, /\{WORKSPACE_SPLITS_ENABLED && \(\s*<ViewModeToggle/)
  // The footer row is a flex row so Settings + toggle sit inline.
  const rule = drawerCss.match(/\.drawer__group--bottom\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /display:\s*flex/)
})

test('the toggle exposes aria-pressed + a mode-specific aria-label and reads as a toggle', () => {
  assert.match(viewModeToggleSrc, /aria-pressed=\{single\}/)
  assert.match(viewModeToggleSrc, /aria-label=\{single \? 'Single screen' : 'Split panes'\}/)
  // Icon reflects the current mode: single square vs split-squares.
  assert.match(viewModeToggleSrc, /single \? <SingleGlyph \/> : <PanesGlyph \/>/)
})

test('the drawer-stays-open contract: the toggle handler never closes the drawer', () => {
  // The toggle button only calls onToggle — no onClose CALL/handler anywhere in
  // the component (prose mentioning onClose is fine; a usage is not).
  assert.match(viewModeToggleSrc, /onClick=\{onToggle\}/)
  assert.doesNotMatch(viewModeToggleSrc, /onClose[=(?]/)
  // And Shell's toggle handler is a pure dispatch — it must not close the drawer.
  const handler = shell.match(/const handleToggleViewMode = useCallback\([\s\S]*?\)\n/)?.[0] || ''
  assert.match(handler, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE', mode: 'toggle' \}\)/)
  assert.doesNotMatch(handler, /closeDrawer/)
})

test('Shell threads viewMode into the content derivation and the per-pane chat gate', () => {
  assert.match(shell, /viewMode: workspace\.viewMode/)
  assert.match(shell, /const \{ multiPane, single, focusedActiveKey, fullBleedKey, visibleAppIds \}/)
  // Single-mode keeps only the focused chat pane doing work.
  assert.match(shell, /chatPanesVisible && \(!single \|\| paneId === workspace\.focusedPaneId\)/)
  // The toggle + vibrate wiring reaches the Drawer and the drag hook.
  assert.match(shell, /onDragBlocked: \(\) => viewModeVibrateRef\.current\?\.\(\)/)
  assert.match(shell, /onToggleViewMode=\{handleToggleViewMode\}/)
  assert.match(shell, /viewModeVibrateRef=\{viewModeVibrateRef\}/)
})

test('the drag binding blocks arming + vibrates in single-mode, and a split-drop flips to panes', () => {
  assert.match(dragBinding, /dragArmingBlocked\(\{ viewMode: wsNow\.viewMode, leafCount: paneIdsInOrder\(wsNow\)\.length \}\)/)
  assert.match(dragBinding, /onDragBlocked\?\.\(\)/)
  // The single-leaf splitting drop opts back into panes.
  assert.match(dragBinding, /wasSingle && target\.edge != null/)
  assert.match(dragBinding, /dispatchWorkspace\(\{ type: 'SET_VIEW_MODE', mode: 'panes' \}\)/)
})

test('the attempted-drag vibrate honors prefers-reduced-motion with a non-motion fallback', () => {
  const shake = drawerCss.match(/\.drawer__viewmode\.is-vibrating\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(shake, /animation:\s*drawer-viewmode-shake/)
  assert.match(drawerCss, /@keyframes drawer-viewmode-shake/)
  // Reduced motion swaps the transform shake for a non-motion outline pulse.
  const reduced = drawerCss.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] || ''
  assert.match(reduced, /drawer-viewmode-pulse/)
  assert.match(reduced, /transform:\s*none/)
  assert.doesNotMatch(
    drawerCss.match(/@keyframes drawer-viewmode-pulse\s*\{[\s\S]*?\}/)?.[0] || '',
    /transform/,
    'the reduced-motion pulse must not animate transform',
  )
})

test('the active (single) toggle state is a quiet accent tint, not a loud fill', () => {
  const rule = drawerCss.match(/\.drawer__viewmode\[aria-pressed="true"\]\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /color:\s*var\(--accent\)/)
  assert.match(rule, /background:\s*var\(--accent-dim\)/)
})
