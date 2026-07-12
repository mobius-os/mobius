# Interactive app building — UX review + remediation program (2026-07-11)

Adversarially verified UX review of the interactive-building work (feature
195 waves 1–2 + the live-preview shell refresh, `347b3d34`…`5b4201df`),
run as a 45-agent workflow: 4 subsystem mappers, 6 review lenses,
per-finding adversarial verification, completeness critic. 33 of 34
findings survived verification. Full narrative report (artifact):
"Möbius — Interactive App Building UX Review" (owner's artifact gallery).

## Verdict

The substrate (atomic generations, apply-on-idle, SW leash, drain-gated
restarts, unified Resume) is solid. The experience lagged it in three
families: the magic moments were mute (app birth = a silent pill; every
recompile blanked the open preview and wiped its state), failures were
silent (`shell_rebuild_failed` dead-wired; failed mini-app compiles
published nothing), and recovery looked like crashing (planned pauses
rendered as danger-red Error cards; the Resume button was below the 44px
touch floor; park reset times showed a bare clock for up-to-7-day parks).

## What shipped in this program (six packages, cross-reviewed)

- session-uxfix-apply (Codex, Opus-reviewed): shell_rebuild_failed wired
  to a toast via the existing summarizer; new `app_build_failed` event
  (system + chat broadcast) with owner-facing compile-error summary;
  idle-apply defers while ANY chat streams; offline guard on the precache
  purge; active voice dictation defers the reload.
- session-uxfix-recovery (Opus, Codex-reviewed): date-aware park reset
  labels (shared `resetTime.js`); park-card reassurance line; `pause_kind`
  benign-pause field end-to-end so planned restarts/stalls render as calm
  "Paused" cards; 44px accent Resume button; offscreen resume nudge;
  interruption-aware SR announcements; push suppressor gated on
  source_type instead of copy matching.
- session-uxfix-reveal (Opus, Codex-reviewed): CTA entrance + press
  feedback; CTA persistence (survives taps + reloads) and multi-app list;
  drawer new-app dot; recompile pulse ("Preview updated ✓"); branded
  first-open loading; chevron unification; reduced-motion spinner
  fallback; light-mode toast shadow.
- session-uxfix-preview-swap (Opus, Codex-reviewed): double-buffered
  iframe version swaps in AppCanvas (`previewSwapState.js` reducer) —
  agent rebuilds update the open mini-app in place, no blank spinner, old
  frame survives a failed/timed-out incoming build.
- session-uxbet-standalone (Opus, Codex-reviewed; PM 214): per-app SSE
  stream `GET /api/apps/{id}/events` (app token sees only its own
  `app_updated`) + "Updated — tap to refresh" pill in the standalone
  shell — cross-device building.
- session-uxbet-milestone (Opus, Codex-reviewed; PM 212): structural
  `build_phase` events (NotifyBody.label, capped, type-gated) + best-
  effort `build_phase.py` helper + seed line + replay-safe live phase
  rail in the building chat.

## Deliberately NOT done

- "Shell updated — tap to apply" toast for successful deferred updates:
  contradicts the owner's quiet-hold call (badge removed, a66b44d7).
  Failure toasts only. Owner may revisit.
- Wave-3 bets, filed as PM cards: 211 split-view build studio, 213
  tap-to-point co-editing, 215 birth reveal + time-lapse replay, 216
  follow-ups (parked-chat drawer state, notification inbox, post-turn CTA
  persistence, CTA intent deep-link).
