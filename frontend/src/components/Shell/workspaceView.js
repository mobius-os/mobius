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

// deriveContentVisibility({ workspace, projection, settingsOverlayOpen,
// immersiveActive, immersiveAppId, viewMode }) → the render flags.
//
// `settingsOverlayOpen` is ONLY the full-workspace Settings TAKEOVER overlay
// (single mode / flag off) — NOT "the focused content is Settings". In builder
// mode Settings is an ordinary pane tab, so this stays FALSE and sibling panes
// keep painting; the Settings wrapper is positioned into its pane rect like any
// chat/app content. Conflating the two would hide every pane in builder (the
// named risk), so this function is deliberately blind to the Settings tab and
// only sees the overlay boolean.
//
// `immersiveActive` already means the holder app is the focused pane's active
// canvas (lib/immersive.isImmersiveActive); `immersiveAppId` is that holder.
// `viewMode` is 'panes' (tiled, the default = BUILDER mode) or 'single' (collapse
// a preserved multi-pane tree to the focused pane's active tab, full-bleed).
//
// ABSOLUTE BUILDER INVARIANT (owner: "no exceptions, no special casing"): in
// builder mode NOTHING renders full-screen — not the Settings overlay, not an
// immersive-solo. Builder mode has exactly ONE rendering path: the tiled/paned
// render. Full-screen takeovers (Settings overlay, immersive-solo, single-mode
// collapse) exist ONLY in single-screen mode. So both the overlay flag and the
// immersive-solo are GATED off in builder here — the invariant is structural,
// not an upstream promise (the nav adapter also keeps settingsOverlayOpen false
// in builder, and Shell keeps immersiveActive false in builder — this is the
// last line of defense so a stray input still can't seize the builder workspace).
export function deriveContentVisibility({
  workspace, projection, settingsOverlayOpen, immersiveActive, immersiveAppId,
  viewMode = 'panes',
}) {
  const multiPane = projection.visibleLeaves.length >= 2
  const builder = viewMode !== 'single'
  // The two full-screen takeovers, forced INERT in builder mode.
  const settingsOverlay = !!settingsOverlayOpen && !builder
  const immersive = !builder && !!immersiveActive && immersiveAppId != null
  // Single view-mode collapse is active only when no takeover already owns the box.
  const single = !builder && !settingsOverlay && !immersive
  // The focused pane's active tab key (null under the Settings overlay — the
  // panes are hidden behind it). In builder mode the overlay is inert, so this is
  // the focused active tab EVEN WHEN that tab is Settings — the Settings wrapper
  // is then the paned (or single-pane full-bleed) surface, driven off this key.
  // In single-mode immersive it is the holder app's key.
  const focusedActiveKey = settingsOverlay
    ? null
    : (workspace.panes[workspace.focusedPaneId]?.activeTabKey ?? null)
  // Pane chrome (strips + dividers) whenever the box is TILED: ≥2 visible leaves
  // and no takeover. In builder this is simply `multiPane` (no takeover can trip
  // here); single-mode / a takeover paints one surface over the whole box.
  const chromeActive = multiPane && !settingsOverlay && !immersive && !single
  // The single wrapper painted full-bleed. Null ONLY in the tiled multi-pane
  // render; under a single-mode collapse / takeover it is the focused/holder key.
  const fullBleedKey = (multiPane && !immersive && !single) ? null : focusedActiveKey
  // The app ids that PAINT and stay interactive/frame-visible. A single-mode
  // immersive solos the holder; single-mode solos the focused pane's active app;
  // the Settings overlay hides all; the tiled (incl. all of builder) render keeps
  // every visible pane's active app. A builder Settings tab is NOT an app, so it
  // contributes no id here — sibling app panes keep painting.
  let visibleAppIds
  if (settingsOverlay) visibleAppIds = new Set()
  else if (immersive) visibleAppIds = new Set([String(immersiveAppId)])
  else if (single) visibleAppIds = paneModel.visibleAppIds(workspace, [workspace.focusedPaneId])
  else visibleAppIds = paneModel.visibleAppIds(workspace, projection.visibleLeaves)
  // Chat panes stay MOUNTED (no remount on overlay/view toggle) but hidden while a
  // takeover owns the box. In builder (never a takeover) and single-mode they
  // paint; the renderer additionally gates each NON-focused single-mode chat pane
  // off via the `single` flag, the chat analogue of visibleAppIds.
  const chatPanesVisible = !settingsOverlay && !immersive
  return {
    multiPane, single, focusedActiveKey, chromeActive, fullBleedKey, visibleAppIds, chatPanesVisible,
  }
}
