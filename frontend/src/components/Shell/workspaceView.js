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

// The presentation key for a null single-screen slot (round 4 item 3). The persisted
// slot stays `null` — adding a `{kind:'new-chat'}` variant would only invent migration
// + sanitizer work — but for RENDERING, null now maps
// to this first-class New Chat landing rather than the freshest chat. `singleScreenRoute`
// still reports {view:'chat', chatId:null}; only the render surface changes.
export const EMPTY_SINGLE_SURFACE_KEY = 'home:new-chat'

// Focus one builder pane without changing the durable split tree. This is a
// presentation projection only: the selected leaf receives the full content box,
// while its tab strip still reserves STRIP_H inside that box. Returning the base
// projection for an invalid id makes pane deletion/collapse self-healing.
export function projectFocusedPane(baseProjection, workspace, paneId, contentRect) {
  if (!paneId || !workspace?.panes?.[paneId]) return baseProjection
  return {
    visibleLeaves: [paneId],
    rects: {
      [paneId]: {
        x: Number.isFinite(contentRect?.x) ? contentRect.x : 0,
        y: Number.isFinite(contentRect?.y) ? contentRect.y : 0,
        w: Math.max(0, Number(contentRect?.w) || 0),
        h: Math.max(0, Number(contentRect?.h) || 0),
      },
    },
    dividers: [],
    // Presentation geometry expands the selected pane, but mode motion still needs
    // its durable position to know which outer edge owns it. Keeping that source
    // rect beside the focused projection avoids a DOM read or a focus-only planner.
    motionRects: baseProjection.rects,
    focusedPaneView: true,
  }
}

// ── Mode scene motion ────────────────────────────────────────────────────────
// Timing and geometry are the only mode-animation facts the React render needs.
// The browser captures settled scenes and owns their lifetime; this module merely
// says where each pane snapshot begins/ends.
export const MODE_MOTION = Object.freeze({
  slideMs: 320,
  logoReleaseMs: 90,
})

// Point a pane completely beyond its nearest outer edges, following its vector
// away from the workspace centre. This is the minimum off-screen vector for that
// pane, used directly by the captured scene.
function offscreenVector(rect, bounds, directionRect = rect) {
  // Shell's live contentRect is intentionally just {w, h}; projection rects are
  // already content-local. Tests and other pure callers may include an origin,
  // so accept both shapes without ever emitting an invalid `NaNpx` CSS variable.
  const boundsX = Number.isFinite(bounds.x) ? bounds.x : 0
  const boundsY = Number.isFinite(bounds.y) ? bounds.y : 0
  const left = rect.x - boundsX
  const top = rect.y - boundsY
  const directionLeft = directionRect.x - boundsX
  const directionTop = directionRect.y - boundsY
  const dx = (directionLeft + directionRect.w / 2) - bounds.w / 2
  const dy = (directionTop + directionRect.h / 2) - bounds.h / 2
  const gap = 24
  let x = 0
  let y = 0
  if (Math.abs(dx) > 1) x = dx < 0 ? -(left + rect.w + gap) : (bounds.w - left + gap)
  if (Math.abs(dy) > 1) y = dy < 0 ? -(top + rect.h + gap) : (bounds.h - top + gap)
  // A truly centred pane still needs a deterministic edge (possible with one
  // tree-absent destination). Top is the least disruptive because the shell bar
  // already establishes that spatial boundary.
  if (x === 0 && y === 0) y = -(top + rect.h + gap)
  return { x, y }
}

function visibleLeafDescriptors(workspace, projection) {
  const out = []
  for (const paneId of projection.visibleLeaves) {
    const pane = workspace.panes[paneId]
    const rect = projection.rects[paneId]
    if (!pane || !pane.activeTabKey || !rect) continue
    out.push({
      paneId,
      activeKey: pane.activeTabKey,
      rect,
      // A focused projection is full-size and centred, which erases the pane's
      // original edge. Direction comes from the durable projection while travel
      // distance comes from the rectangle that is actually painted.
      motionRect: projection.motionRects?.[paneId] || rect,
    })
  }
  return out
}

// The native mode transition moves settled pane snapshots, not live surfaces.
// Every visible pane begins one small gap beyond its own outer edges and lands on
// the same linear clock. Keeping the minimum pane-owned vector makes first contact
// with the viewport consistent across unequal layouts.
export function deriveModeSnapshotPlan({ workspace, projection, contentRect }) {
  if (!workspace || !projection || !contentRect) return null
  const offsets = {}
  for (const leaf of visibleLeafDescriptors(workspace, projection)) {
    offsets[leaf.paneId] = offscreenVector(
      leaf.rect,
      contentRect,
      leaf.motionRect,
    )
  }
  return Object.keys(offsets).length > 0
    ? { offsets, totalMs: MODE_MOTION.slideMs }
    : null
}

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
// Builder's durable world has exactly one structural rendering path: its pane
// tree is never collapsed or rewritten by Settings. Immersive is deliberately
// different: it is a temporary verified app lease layered OVER either world.
// While held it solos the focused app; clearing it re-derives the untouched pane
// tree immediately. Settings remains mode-gated and cannot become a builder
// takeover, preserving that invariant without making a game's explicit Focus
// control silently inert whenever the owner happens to be in Builder mode.
export function deriveContentVisibility({
  workspace, projection, settingsOverlayOpen, immersiveActive, immersiveAppId,
  viewMode = 'panes', focusedPaneView = false,
}) {
  const multiPane = projection.visibleLeaves.length >= 2
  const builder = viewMode !== 'single'
  // Settings is structurally inert in builder. Immersive is a temporary overlay
  // lease and may cover either world without mutating it.
  const settingsOverlay = !!settingsOverlayOpen && !builder
  const immersive = !!immersiveActive && immersiveAppId != null
  // Single view-mode collapse is active only when no takeover already owns the box.
  const single = !builder && !settingsOverlay && !immersive
  // TWO-WORLDS (codex-modecontext-design.md): in SINGLE mode the active content is
  // the persisted single-screen SLOT — the last item opened IN single mode — NOT
  // the focused builder pane. The slot may be absent from the pane tree entirely;
  // Shell pins its iframe / chat mount regardless. A null OR absent slot is the empty
  // New Chat landing (singleScreenKey → null): Standard has its OWN memory and never
  // borrows Builder's focus, so an uninitialized Standard paints the landing rather
  // than the focused pane. In BUILDER mode all of this is null and the focused-pane
  // path runs unchanged.
  const focusedPaneKey = workspace.panes[workspace.focusedPaneId]?.activeTabKey ?? null
  const slotKey = single ? paneModel.singleScreenKey(workspace) : null
  const emptySingleSlot = single && slotKey == null
  // The active tab key that drives the full-bleed surface + AppCanvas `active`
  // prop. Under the Settings overlay it is null (panes hidden behind it). In single
  // mode it is the slot key (or the focused-pane fallback); otherwise the focused
  // pane's active tab — EVEN WHEN that is Settings (a builder Settings tab is the
  // paned/full-bleed surface, driven off this key). Immersive uses the holder key.
  // A null slot keeps focusedActiveKey NULL so navigation + AppCanvas never pretend
  // the New Chat landing is a chat/app tab (the landing is not a tab).
  const focusedActiveKey = settingsOverlay
    ? null
    : (immersive ? `app:${immersiveAppId}` : (single ? slotKey : focusedPaneKey))
  // Pane chrome (strips + dividers) whenever the box is TILED: ≥2 visible leaves
  // and no takeover. A focused builder projection retains its single pane's strip;
  // single mode or an immersive lease paints one surface over the whole box.
  const chromeActive = (multiPane || (builder && focusedPaneView))
    && !settingsOverlay && !immersive && !single
  // The single wrapper painted full-bleed. Null ONLY in the tiled multi-pane render;
  // the New Chat landing key for an empty single slot; the focused/holder key
  // otherwise. Distinct from focusedActiveKey (which stays null for the empty slot)
  // so the render paints the landing while nav/AppCanvas see no active tab.
  const fullBleedKey = focusedPaneView && builder
    ? null
    : emptySingleSlot
    ? EMPTY_SINGLE_SURFACE_KEY
    : ((multiPane && !immersive && !single) ? null : focusedActiveKey)
  // The app ids that PAINT and stay interactive/frame-visible. A single-mode
  // immersive solos the holder; single-mode solos the focused pane's active app;
  // the Settings overlay hides all; an ordinary tiled builder render keeps every
  // visible pane's active app. A builder Settings tab is NOT an app, so it
  // contributes no id here — sibling app panes keep painting.
  let visibleAppIds
  if (settingsOverlay) visibleAppIds = new Set()
  else if (immersive) visibleAppIds = new Set([String(immersiveAppId)])
  else if (single) {
    // The single world paints ONLY the slot; if the slot is an app, that one app
    // is visible (two-worlds design). A chat / empty / absent slot paints no app —
    // Standard never borrows the focused Builder pane's app. The slot may be
    // tree-absent, so read it directly.
    const slot = workspace.singleScreen
    visibleAppIds = (slot && slot.kind === 'app') ? new Set([String(slot.id)]) : new Set()
  } else visibleAppIds = paneModel.visibleAppIds(workspace, projection.visibleLeaves)
  // Chat panes stay MOUNTED (no remount on overlay/view toggle) but hidden while a
  // takeover owns the box. In an ordinary builder world and single-mode they
  // paint; the renderer additionally gates each NON-focused single-mode chat pane
  // off via the `single` flag, the chat analogue of visibleAppIds.
  const chatPanesVisible = !settingsOverlay && !immersive
  return {
    // `settingsOverlay` is the EFFECTIVE-mode-gated takeover flag (finding F3): it
    // is the one honest "is the Settings takeover painting NOW" signal — false in
    // builder AND during a single-mode drag preview (viewMode='panes').
    // Shell's PAINT gates read THIS, not the committed-gated nav flag, so the tiled
    // world paints with the takeover suspended exactly as the flags above assume.
    multiPane, single, focusedActiveKey, chromeActive, fullBleedKey, visibleAppIds,
    chatPanesVisible, settingsOverlay,
  }
}
