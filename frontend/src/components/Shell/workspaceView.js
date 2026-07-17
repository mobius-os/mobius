// Content-visibility derivation for the shell render (design §2/§4/§5).
//
// The renderer positions a flat, never-reparented set of content wrappers into
// pane rectangles. WHICH wrapper is painted, WHERE, and whether the pane chrome
// shows are all functions of the projection plus two overlay states — Settings
// and immersive. This module is that function, pulled out of Shell.jsx so the
// two overlay branches (especially immersive-solo, which had no multi-pane
// coverage) are unit-testable without a DOM.
//
// Immersive solos its pane over the WHOLE workspace (design §4/§9): the chrome
// is hidden, the holder app is painted full-bleed over the entire content box,
// and every sibling — app frames and chat panes alike — is hidden so it stops
// painting and receives frame-visibility:false. Exit restores the tree exactly
// because immersive is separate state that never mutates the workspace, so
// clearing it re-derives the ordinary multi-pane view with no remount.

import * as paneModel from './paneModel.js'

// deriveContentVisibility({ workspace, projection, settingsActive, immersiveActive,
// immersiveAppId, viewMode }) → the render flags. `immersiveActive` already means
// the holder app is the focused pane's active canvas (lib/immersive.isImmersiveActive);
// `immersiveAppId` is that holder. `viewMode` is 'panes' (tiled, the default) or
// 'single' (collapse a preserved multi-pane tree to the focused pane's active tab,
// full-bleed). viewMode is ORTHOGONAL to the two overlays and yields to them —
// while Settings or immersive owns the whole box, single-mode has no effect.
export function deriveContentVisibility({
  workspace, projection, settingsActive, immersiveActive, immersiveAppId,
  viewMode = 'panes',
}) {
  const multiPane = projection.visibleLeaves.length >= 2
  const immersive = !!immersiveActive && immersiveAppId != null
  // Single view-mode is active only when no overlay owns the box: Settings and
  // immersive each already solo/hide the whole content area, so they take
  // precedence and single-mode composes to a no-op under either.
  const single = !settingsActive && !immersive && viewMode === 'single'
  // The focused pane's active tab key (null under Settings). In immersive this
  // is the holder app's key, and in single-mode it is the one surface painted
  // full-bleed, so it drives the full-bleed choice below.
  const focusedActiveKey = settingsActive
    ? null
    : (workspace.panes[workspace.focusedPaneId]?.activeTabKey ?? null)
  // Pane chrome (strips + dividers) only in the ordinary TILED render: ≥2 visible
  // leaves, no overlay, and NOT single-mode. Single-mode paints one surface over
  // the whole box, so there is nothing to tile and no chrome to draw.
  const chromeActive = multiPane && !settingsActive && !immersive && !single
  // The single wrapper painted full-bleed. Null ONLY in the tiled multi-pane
  // render (each active tab is positioned into its pane rect); in single-pane,
  // single view-mode, under Settings-hidden, or immersive it is the focused/holder
  // key painted over the whole box.
  const fullBleedKey = (multiPane && !immersive && !single) ? null : focusedActiveKey
  // The app ids that PAINT and stay interactive/frame-visible. Immersive solos
  // the holder; single-mode solos the focused pane's active app (every sibling
  // frame goes visibility:false); Settings hides all; the tiled render keeps
  // every visible pane's active app.
  let visibleAppIds
  if (settingsActive) visibleAppIds = new Set()
  else if (immersive) visibleAppIds = new Set([String(immersiveAppId)])
  else if (single) visibleAppIds = paneModel.visibleAppIds(workspace, [workspace.focusedPaneId])
  else visibleAppIds = paneModel.visibleAppIds(workspace, projection.visibleLeaves)
  // Chat panes stay MOUNTED (no remount on overlay/view toggle) but hidden while
  // an overlay owns the box — an immersive holder is always an app, so no chat
  // pane is ever the solo surface. In single-mode this stays true (the focused
  // chat pane still paints); the renderer additionally gates each NON-focused
  // chat pane off via the `single` flag below, the chat analogue of visibleAppIds.
  const chatPanesVisible = !settingsActive && !immersive
  return {
    multiPane, single, focusedActiveKey, chromeActive, fullBleedKey, visibleAppIds, chatPanesVisible,
  }
}
