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
import * as tabModel from './tabModel.js'

// The presentation key for a null single-screen slot (round 4 item 3). The persisted
// slot stays `null` — adding a `{kind:'new-chat'}` variant would only invent migration
// + sanitizer work — but for RENDERING and for the exit target/underlay, null now maps
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

// ── Mode-transition motion (exit-presentation v2) ────────────────────────────
// The presentation module owns the timing + the pure plan builders; the state
// machine (modeMachine.js) treats a plan as OPAQUE data. A beat is described by a
// latched plan of participants and their compositor-only motions — never a role
// enum the machine has to grow a branch for. A future destination surface needs
// only a target adapter here plus renderer support; the machine is unchanged.
//
// Timing (ms). Constants live here, NOT in the machine, so the reconcile clock
// (INV 14) and the missing-target fallback (INV 13) reason about the plan's own
// totalMs rather than a fixed per-phase maximum. Mode changes are intentionally one
// short beat: every pane moves together, using only compositor transforms + opacity.
// There is no per-pane stagger or second destination phase to make the owner wait.
export const MODE_MOTION = Object.freeze({
  enterItemMs: 210,
  exitItemMs: 180,
  promoteMs: 210,
  logoReleaseMs: 90,
})

// The slack a visibility-return reconcile allows past the plan's totalMs before it
// force-completes a beat whose animationend never arrived (hidden tab, throttled
// rAF). Not a correctness timer — nothing fires it on its own; it is a pure
// comparison against startedAt at the reconcile boundary (INV 14).
export const RECONCILE_SLACK_MS = 250

// The CSS animation-names each phase's completion listens for. Kept beside the
// keyframes so a name typo is caught by one grep. Strip-clear + chrome fades are
// deliberately ABSENT: they are shorter and must not gate completion.
export const PROMOTE_NAME = 'shell-mode-promote'
export const DEAL_OUT_NAME = 'shell-mode-deal-out'
export const DEAL_IN_NAME = 'shell-mode-deal-in'

// A pane's CONTENT rect is its pane rect minus the strip row on top — the same
// geometry the tiled render positions the wrapper into (see visibleTabRects).
function contentRectOfPane(rect) {
  return { x: rect.x, y: rect.y + paneModel.STRIP_H, w: rect.w, h: Math.max(0, rect.h - paneModel.STRIP_H) }
}

// The FLIP the promote pane runs: it stays at its tiled content rect and
// transforms to cover the full destination. Computed ONCE from the projection
// authority (never DOM reads) and latched, so a mid-beat layout change cannot jerk
// a live transform (INV 5/10) — a snapshot mismatch cancels instead.
function flipTo(from, dest) {
  return {
    x: -from.x,
    y: -from.y,
    sx: from.w ? dest.w / from.w : 1,
    sy: from.h ? dest.h / from.h : 1,
  }
}

// Push a pane just beyond the nearest outer edges, following its vector away from
// the workspace centre. A left/right split therefore travels horizontally, a
// top/bottom split vertically, and a corner pane diagonally. The resulting motion
// reads as one assembled surface rather than four unrelated card transitions.
// Values are projection-derived and latched with the plan — no DOM measurement.
function edgeOffset(rect, bounds, directionRect = rect) {
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

// A normal one-leaf builder uses the flow tab strip outside .shell__content, so
// its wrapper already fills the content box. A focused pane is also a one-leaf
// projection, but its WorkspaceChrome strip remains inside the pane. Projection
// metadata makes that structural difference explicit instead of inferring it from
// leaf count alone.
function paintedContentRect(leaf, projection, leafCount) {
  return (leafCount > 1 || projection.focusedPaneView)
    ? contentRectOfPane(leaf.rect)
    : leaf.rect
}

// The concrete surface key SINGLE mode will paint after this exit — the slot, the New
// Chat landing (an explicit null slot, round 4 item 3), or (legacy absent-slot blob)
// the value the SAME SET_VIEW_MODE transaction will seed from the focused item. A
// Settings-focused legacy seed still resolves to null. The one classification input.
function exitTargetKey(ws) {
  // An INITIALIZED slot: a concrete chat/app key, or the New Chat landing when null.
  // Null now means a definite New Chat destination — never the freshest chat.
  if ('singleScreen' in ws) return paneModel.singleScreenKey(ws) || EMPTY_SINGLE_SURFACE_KEY
  const seed = paneModel.focusedSlotSeed(ws)
  if (!seed) return null
  return seed.kind === 'app' ? `app:${seed.id}` : `chat:${seed.id}`
}

// The currently-painted visible leaves (paneId + active key + pane rect), in the
// order the projection lists them. The membership + active keys are the topology
// facts the snapshot signature latches; any change cancels the beat (INV 10).
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

// The effective destination single mode will ACTUALLY paint on completion, given the
// tree's slot plus the two live overlay states (M2). A suspended Settings takeover
// paints full-bleed OVER the slot (reveal to Settings); a retained immersive holder
// that solos the exit slot is an INSTANT destination (full-viewport, header gone) the
// beat cannot honestly latch. Both the exit PLAN and the exit SIGNATURE classify
// through THIS one function from the SAME input, so an overlay input can never change
// the plan's destination without also changing its invalidation key (INV 10 / H2).
function classifyExitDestination({ workspace, settingsDestination = false, immersiveHolderId = null }) {
  const slotTarget = exitTargetKey(workspace)
  const immersiveInstant = !settingsDestination
    && immersiveHolderId != null
    && slotTarget === `app:${immersiveHolderId}`
  const target = settingsDestination ? tabModel.SETTINGS_TAB_KEY : slotTarget
  return { target, immersiveInstant }
}

// The immutable invalidation key for either directional beat. INVARIANT (INV 10 /
// H2): it incorporates EVERY input deriveExitPlan / deriveEnterPlan uses — visible
// pane keys and rects, content bounds, and the effective destination (including live
// Settings/immersive state). The controller recomputes it and cancels on ANY drift;
// otherwise a live layout commit could move wrappers underneath stale FLIP/edge
// transforms. New destination inputs belong in classifyExitDestination, shared by
// this signature and both plan builders, never in one alone.
export function transitionSignature(input) {
  const { workspace, projection, contentRect } = input
  const { target, immersiveInstant } = classifyExitDestination(input)
  const leaves = visibleLeafDescriptors(workspace, projection)
    .map((l) => {
      const painted = `${l.rect.x},${l.rect.y},${l.rect.w},${l.rect.h}`
      if (l.motionRect === l.rect) return `${l.paneId}=${l.activeKey}@${painted}`
      const motion = `${l.motionRect.x},${l.motionRect.y},${l.motionRect.w},${l.motionRect.h}`
      return `${l.paneId}=${l.activeKey}@${painted}~${motion}`
    })
  const x = Number.isFinite(contentRect.x) ? contentRect.x : 0
  const y = Number.isFinite(contentRect.y) ? contentRect.y : 0
  return `${target || ''}|${immersiveInstant ? 'i' : ''}|${leaves.join(',')}`
    + `|${x},${y},${contentRect.w}x${contentRect.h}`
}

// Sort by visual reading order (top, then left) of the pane rect.
function byVisualOrder(a, b) {
  if (a.rect.y !== b.rect.y) return a.rect.y - b.rect.y
  return a.rect.x - b.rect.x
}

// deriveExitPlan(input) → the latched exit plan, or null when the beat is instant (an
// empty tree — nothing painted to deal out — or an immersive-instant destination).
// `input` is { workspace, projection, contentRect, settingsDestination?,
// immersiveHolderId? }; the SAME object is fed to transitionSignature so the plan
// and its invalidation key can never disagree about the destination (INV 10 / H2).
//
// Classification (exit-design v1 §exit-classification, honored by v2):
//   - target is the active key of a VISIBLE leaf → promote that leaf (physical
//     continuity), deal every sibling out, no underlay.
//   - target is inactive-in-a-pane, tree-absent, the New Chat landing (an empty single
//     slot, round 4 item 3), or null → WORLD REVEAL: deal every painted leaf out over
//     the mounted destination (underlayKey = target; null = the opaque background only
//     for a legacy Settings-focused absent-slot). Never promote the focused pane to
//     manufacture a correspondence single mode will not paint.
export function deriveExitPlan(input) {
  const { workspace, projection, contentRect, settingsDestination = false } = input
  const leaves = visibleLeafDescriptors(workspace, projection)
  if (leaves.length === 0) return null // empty tree → instant flip, no descriptor
  // HONEST DESTINATION (M2): what single mode will ACTUALLY paint on completion — the
  // tree's slot re-classified by the live overlays — never the slot the tree seeds
  // beneath a takeover/immersive-solo (else the takeover/immersive pops over the
  // promoted-or-revealed slot at completion, breaking the "visually identical
  // completion" contract). The exit signature reads the SAME classifier (H2).
  //   - An immersive holder that solos the exit slot is a full-viewport INSTANT the
  //     beat cannot honestly latch while the header is still painted and the mode is
  //     still 'panes'. An honest instant beats a false animation that jumps at
  //     completion, so classify it instant (return null).
  const { target, immersiveInstant } = classifyExitDestination(input)
  if (immersiveInstant) return null
  //   - A suspended Settings takeover paints full-bleed OVER the slot → world reveal
  //     to the mounted-hidden Settings surface (part-2 F3), never the slot the
  //     takeover then covers (target = SETTINGS_TAB_KEY). modeMachine stays ignorant
  //     of what Settings means; the underlayKey just names the destination wrapper.
  const dest = { x: 0, y: 0, w: contentRect.w, h: contentRect.h }
  // A Settings destination is always a world reveal (never a promote), even if a
  // builder Settings tab happens to be a visible leaf.
  const promoteLeaf = (target && !settingsDestination) ? leaves.find(l => l.activeKey === target) : null

  const participants = []
  const completionNames = new Set()
  let underlayKey = null

  if (promoteLeaf) {
    // FLIP the promote pane from its ACTUAL wrapper geometry to the full box. At a
    // single visible leaf the strip is a flex SIBLING outside .shell__content, so the
    // sole wrapper already fills the content box — its rect IS the destination, an
    // identity FLIP with no STRIP_H inset. At >=2 leaves the WorkspaceChrome strips
    // sit INSIDE the pane rect, so the wrapper is inset by STRIP_H. Insetting the
    // single-leaf case (contentRectOfPane) overshot the FLIP (y:-STRIP_H, sy>1) and
    // snapped back when the strip unmounted (M4).
    const siblings = leaves.filter(l => l !== promoteLeaf).sort(byVisualOrder)
    const fromRect = paintedContentRect(promoteLeaf, projection, leaves.length)
    participants.push({
      key: promoteLeaf.activeKey,
      paneId: promoteLeaf.paneId,
      motion: 'promote',
      delayMs: 0,
      durationMs: MODE_MOTION.promoteMs,
      flip: flipTo(fromRect, dest),
    })
    completionNames.add(PROMOTE_NAME)
    // Siblings deal out in visual order beneath the promoting pane.
    siblings.forEach((l) => {
      participants.push({
        key: l.activeKey, paneId: l.paneId, motion: 'deal-out',
        delayMs: 0, durationMs: MODE_MOTION.exitItemMs,
        offset: edgeOffset(l.rect, contentRect, l.motionRect),
      })
    })
    if (siblings.length) completionNames.add(DEAL_OUT_NAME)
  } else {
    // World reveal: every painted leaf deals out together over the stationary target.
    underlayKey = target // null = home reveal (opaque --bg background)
    // The underlay is the stationary DESTINATION, so a visible leaf that IS the
    // underlay (a builder Settings tab equal to the takeover destination) never
    // also deals out. For an ordinary tree-absent chat/app slot this filters
    // nothing — it was not a visible leaf, or the promote branch would have claimed
    // it. If it leaves nothing to deal out (destination is the sole surface), the
    // beat has no honest motion → instant flip.
    const ordered = leaves.filter(l => l.activeKey !== target).sort(byVisualOrder)
    if (ordered.length === 0) return null
    ordered.forEach((l) => {
      participants.push({
        key: l.activeKey, paneId: l.paneId, motion: 'deal-out',
        delayMs: 0, durationMs: MODE_MOTION.exitItemMs,
        offset: edgeOffset(l.rect, contentRect, l.motionRect),
      })
    })
    completionNames.add(DEAL_OUT_NAME)
  }

  const totalMs = participants.reduce((m, p) => Math.max(m, p.delayMs + p.durationMs), 0)
  return {
    kind: 'exit',
    target,
    destinationRect: dest,
    participants,
    underlayKey,
    completionNames: [...completionNames],
    totalMs,
    snapshotSignature: transitionSignature(input),
  }
}

// deriveEnterPlan(input) → the latched entry plan, or null when there is nothing
// to assemble. The single-screen surface remains a stationary full-bleed underlay
// while every OTHER visible pane gathers from its nearest outer edge. When that
// surface is also a builder leaf, completion simply crops the retained wrapper
// into its pane. This keeps the Standard world visually still instead of scaling
// it like a foreground card, and needs no duplicate ChatView/AppCanvas.
//
export function deriveEnterPlan(input) {
  const { workspace, projection, contentRect } = input
  const leaves = visibleLeafDescriptors(workspace, projection).sort(byVisualOrder)
  if (leaves.length === 0) return null
  const { target, immersiveInstant } = classifyExitDestination(input)
  if (immersiveInstant) return null
  const duration = MODE_MOTION.enterItemMs
  const destinationRect = { x: 0, y: 0, w: contentRect.w, h: contentRect.h }
  const participants = []
  const completionNames = new Set()
  const underlayKey = target

  for (const l of leaves) {
    if (l.activeKey === target) continue
    participants.push({
      key: l.activeKey, paneId: l.paneId, motion: 'deal-in',
      delayMs: 0,
      durationMs: duration,
      offset: edgeOffset(l.rect, contentRect, l.motionRect),
    })
    completionNames.add(DEAL_IN_NAME)
  }
  if (participants.length === 0) return null
  const totalMs = participants.reduce((m, p) => Math.max(m, p.delayMs + p.durationMs), 0)
  return {
    kind: 'enter',
    target,
    destinationRect,
    participants,
    underlayKey,
    completionNames: [...completionNames],
    totalMs,
    snapshotSignature: transitionSignature(input),
  }
}

// deriveContentVisibility({ workspace, projection, settingsOverlayOpen,
// immersiveActive, immersiveAppId, viewMode, exitUnderlayKey }) → the render flags.
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
  viewMode = 'panes', exitUnderlayKey = null, focusedPaneView = false,
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
  // Shell pins its iframe / chat mount regardless. A null slot is the New Chat landing
  // (round 4 item 3). BACKWARD-COMPAT: a blob whose slot property is ABSENT is legacy/
  // uninitialized (the reducer seeds it on the first builder→single switch, using
  // absence as the migration marker), so single mode falls back to the focused
  // pane's active tab until the slot is seeded — an older blob still collapses to
  // the focused surface exactly as before. In BUILDER mode all of this is null and
  // the focused-pane path runs unchanged.
  const hasSlot = ('singleScreen' in workspace)
  const focusedPaneKey = workspace.panes[workspace.focusedPaneId]?.activeTabKey ?? null
  const slotKey = single ? (hasSlot ? paneModel.singleScreenKey(workspace) : focusedPaneKey) : null
  // An INITIALIZED but empty slot in single mode is the New Chat landing (round 4
  // item 3): a first-class home:new-chat surface, never the freshest chat. Legacy
  // absent-slot blobs still fall back to the focused pane (hasSlot false).
  const emptySingleSlot = single && hasSlot && paneModel.singleScreenKey(workspace) == null
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
    // is visible (two-worlds design). A chat/empty slot paints no app. The slot may
    // be tree-absent, so read it directly. Legacy (absent-slot) blobs fall back to
    // the focused pane, matching the pre-two-worlds single-mode collapse.
    if (hasSlot) {
      const slot = workspace.singleScreen
      visibleAppIds = (slot && slot.kind === 'app') ? new Set([String(slot.id)]) : new Set()
    } else {
      visibleAppIds = paneModel.visibleAppIds(workspace, [workspace.focusedPaneId])
    }
  } else visibleAppIds = paneModel.visibleAppIds(workspace, projection.visibleLeaves)
  // EXIT-BEAT UNDERLAY (exit-presentation v2): a WORLD-REVEAL exit paints the
  // already-mounted destination full-bleed BENEATH the dealing-out tree while the
  // effective mode is still 'panes'. Its app is not a visible tree pane, so union
  // it in here — otherwise the underlay would show a blank frame. A chat/home
  // underlay contributes no app id; Shell paints the chat wrapper directly.
  if (exitUnderlayKey && exitUnderlayKey.startsWith('app:')) {
    visibleAppIds = new Set(visibleAppIds)
    visibleAppIds.add(exitUnderlayKey.slice('app:'.length))
  }
  // Chat panes stay MOUNTED (no remount on overlay/view toggle) but hidden while a
  // takeover owns the box. In an ordinary builder world and single-mode they
  // paint; the renderer additionally gates each NON-focused single-mode chat pane
  // off via the `single` flag, the chat analogue of visibleAppIds.
  const chatPanesVisible = !settingsOverlay && !immersive
  return {
    // `settingsOverlay` is the EFFECTIVE-mode-gated takeover flag (finding F3): it
    // is the one honest "is the Settings takeover painting NOW" signal — false in
    // builder AND during a single-mode drag preview / exit beat (viewMode='panes').
    // Shell's PAINT gates read THIS, not the committed-gated nav flag, so the tiled
    // world paints with the takeover suspended exactly as the flags above assume.
    multiPane, single, focusedActiveKey, chromeActive, fullBleedKey, visibleAppIds,
    chatPanesVisible, settingsOverlay, exitUnderlayKey,
  }
}
