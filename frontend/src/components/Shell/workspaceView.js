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
// immersiveAppId }) → the render flags. `immersiveActive` already means the
// holder app is the focused pane's active canvas (lib/immersive.isImmersiveActive);
// `immersiveAppId` is that holder.
export function deriveContentVisibility({
  workspace, projection, settingsActive, immersiveActive, immersiveAppId,
}) {
  const multiPane = projection.visibleLeaves.length >= 2
  const immersive = !!immersiveActive && immersiveAppId != null
  // The focused pane's active tab key (null under Settings). In immersive this
  // is the holder app's key, so it drives the full-bleed choice below.
  const focusedActiveKey = settingsActive
    ? null
    : (workspace.panes[workspace.focusedPaneId]?.activeTabKey ?? null)
  // Pane chrome only at ≥2 visible leaves, and never while an overlay (Settings
  // or immersive) owns the whole content box.
  const chromeActive = multiPane && !settingsActive && !immersive
  // The single wrapper painted full-bleed. Null in ordinary multi-pane (each
  // active tab is positioned into its pane rect); in single-pane, under
  // Settings-hidden, or immersive it is the focused/holder key painted over the
  // whole box.
  const fullBleedKey = (multiPane && !immersive) ? null : focusedActiveKey
  // The app ids that PAINT and stay interactive/frame-visible. Immersive solos
  // the holder — every sibling frame goes visibility:false — and Settings hides
  // all of them.
  let visibleAppIds
  if (settingsActive) visibleAppIds = new Set()
  else if (immersive) visibleAppIds = new Set([String(immersiveAppId)])
  else visibleAppIds = paneModel.visibleAppIds(workspace, projection.visibleLeaves)
  // Chat panes stay MOUNTED (no remount on overlay toggle) but hidden while an
  // overlay owns the box — an immersive holder is always an app, so no chat pane
  // is ever the solo surface.
  const chatPanesVisible = !settingsActive && !immersive
  return {
    multiPane, focusedActiveKey, chromeActive, fullBleedKey, visibleAppIds, chatPanesVisible,
  }
}
