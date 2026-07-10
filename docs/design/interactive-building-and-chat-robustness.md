# Interactive building & chat robustness — serving, updates, turn lifecycle

Owner-directed design (2026-07-10). Fix-forward; no backwards compatibility.
Produced by a two-arm design ensemble (robustness-first + simplest-fits),
synthesized by the main agent, then amended by an adversarial review whose
8 findings are folded in below (marked ⚑ where they changed the design).

## The evidence this design answers

Prod, recent week: 86/88 turns completed — the runner state machine is
stable. What actually reaches the owner as breakage:

- **16 error blocks** "the previous turn was interrupted (the server
  restarted)" — our restart-heavy workflow kills live turns. Queued messages
  are preserved (fixed earlier), but the live turn dies and nothing resumes.
- **15 error blocks** provider usage/session limits — external, but the text
  carries the reset time and we leave a dead tombstone.
- **1 wedged turn** (>1h, `event_count` frozen at 1599, 0 subscribers) — no
  runner-level liveness watchdog exists.
- **Visual-breakage class**: every `frontend_watcher` build broadcasts
  `shell_rebuilt` → every open PWA force-reloads (jarring mid-turn); each
  build is a cold `npx vite build` (~25s); after a dist swap an unreloaded
  tab's lazy chunks 404 (old hashed assets deleted) and missing assets fall
  through to SPA HTML ("failed to load module"); a dist appearing after boot
  needs a restart to serve (module-load static-dir); the SW eagerly
  `skipWaiting()`+`clientsClaim()`s and `index.html` reloads on
  `controllerchange` — so even a deferred page reload can be preempted by the
  SW generation flipping underneath it. ⚑

## Design invariants (the bar)

1. **The streaming view is sacred.** Nothing reloads or blanks a live turn —
   not a rebuild, not an SW activation, not a restart.
2. **A rebuild is never disruptive.** Open tabs keep working on their old
   generation until they apply the new one at an idle boundary.
3. **Nothing the owner sent is ever lost.** Turns drain or park with partials
   persisted; queues persist. **Nothing "dead" springs back to life
   unrequested** — resumption is one explicit tap. ⚑
4. Philosophy: serving/update/turn plumbing is invisible substrate — ironclad
   engineering belongs here. Deploy-shaped steps stay explicit; tool-shipped
   automation (watch mode, SW lifecycle) is fine; no custom detect-and-auto-do.

## Part 1 — Serving & updates (interactive building)

### 1.1 Generations: warm build → published generation → attic

Three layers replace today's build-equals-publish-equals-reload coupling:

- **Warm builds** (`vite build --watch`): one long-lived watch process keeps
  the module graph warm (~1–3s incrementals, bursts coalesced) and writes to
  an internal **staging** output — never directly to the served tree. ⚑
- **Publication** is a separate, atomic step: promote staging →
  `.dist-next` → validate → swap to `dist` **as one consistent generation**
  (assets + `index.html` + `sw.js` together), then emit ONE
  `shell_generation {id}` event. Publication happens on a settle (edits
  stopped for a few seconds) or on the agent's explicit `shell_apply_now`.
  Ten edits in a burst → many warm builds, **one** publication, one signal.
  This closes the multi-rebuild SW install race (a new `sw.js` can no longer
  be fetched while a *different* generation's `index.html` swaps in — the
  window between generations is one atomic publish). ⚑
- **Attic**: on each publish, hardlink the outgoing `dist/assets/*` AND the
  outgoing `index.html` (as `index-<gen>.html`) into `.assets-attic/`;
  prune to the last K=3 generations. Content-hashed names never collide, so
  a flat union pool is safe. Gitignored, near-free (hardlinks). ⚑ (index
  retained too, not just assets — the generation boundary covers every
  entry file: assets from the attic, `index.html` per generation, `sw.js` +
  manifest always `no-cache`, vendor is baked-stable via the existing
  versioned symlink aliases.)

### 1.2 Request-time serving: current-then-attic, 404s are 404s

Replace the module-load `_static_dir` constant and the `StaticFiles("/assets")`
mount with request-time resolution:

- `/assets/<f>`: serve from `dist/assets`, else from the attic (immutable
  cache headers), else **404 — never the SPA HTML fallback** (a missing chunk
  must be a 404, not a mystery HTML payload).
- SPA fallback: resolve `dist if complete else baked` per request (cheap
  stat + short-TTL cache).

Consequences: an open tab never 404s its chunk graph; old and new module
graphs never mix (an unreloaded tab stays wholly on its generation); a dist
appearing or rebuilding after boot is served live — **restart is never needed
for frontend changes**; the baked floor becomes a per-request corruption
floor rather than a "you must restart" trap.

Sizing note: this touches the `/assets` mount, SPA fallback, `/sw.js` +
manifest routes, and swap atomicity — it is an L with the SW work, not an
M. ⚑

### 1.3 Client update lifecycle: apply-on-idle, SW on a leash

Replace `shell_rebuilt` → unconditional `location.reload()` with a client
state machine in Shell.jsx (it already tracks streaming state):

- **idle** + new generation → apply (reload) immediately — feels live while
  the owner is watching a build session.
- **streaming / composing / unanswered question / send in flight** → defer;
  on turn `done`, a "Shell updated — tap to apply" toast; auto-apply at the
  next idle/navigation. The attic makes deferral safe indefinitely.
- A second publication while one is deferred simply replaces the pending
  generation — the client applies the LATEST, once. ⚑
- **SW lifecycle migration (load-bearing, same work item)** ⚑: remove
  top-level `skipWaiting()`/`clientsClaim()` from `sw.js`; the new SW
  installs and WAITS; the page sends `SKIP_WAITING` only at the idle-apply
  moment, so the SW generation flips exactly when the page generation does.
  Disable the `controllerchange` auto-reload and the stale-precache forced
  reload while non-idle. Test: "new SW found during streaming does not
  reload the page or delete a cache the current page needs."
- Backend/platform restart-needed → the existing Settings prompt; the
  restart itself is drain-gated (Part 2); the client shows "applying
  update…" and the SSE catch-up reconstitutes the stream after boot.

State that survives an apply (already built): sessionStorage view snapshot,
chat scroll, stream snapshot + SSE catch-up replay.

### 1.4 The interactive tiers, summarized

| Tier | Mechanism | Latency | Open client mid-turn |
|---|---|---|---|
| Agent edits frontend | warm watch build → settle/`shell_apply_now` publish | ~1–3s build, applied at idle | never interrupted; toast at turn end |
| Backend edit | explicit restart prompt → drain-gated restart | ~5s + drain | turn drains/parks; stream reconstitutes |
| Platform/git update, image deploy | explicit Update/apply flow → publish + drain-gated restart | minutes | same as backend |

HMR/dev-server (sub-100ms, no reload at all) remains the named FUTURE tier:
it forks the serving path (second server, websocket through Caddy, dev/prod
bundle divergence, SW interplay). Adopt deliberately only if watch-mode
publishing doesn't feel live enough in practice.

### 1.5 The agent affordance (instructed, not detected)

`shell_apply_now`: an explicit endpoint/script the agent calls when an edit
batch is coherent — "I'm done, look now" — triggering a publication and an
idle-apply request. One seed line: *after a burst of shell edits, signal
apply so the owner sees them together.* No auto-detection anywhere.

## Part 2 — Turn lifecycle (chat that stops breaking)

### 2.1 Explicit run-states ⚑

The substrate names what a live turn is doing — one owner per running turn:

`streaming | tool_running | waiting_for_user (AskUserQuestion) |
provider_parked | draining | parked_for_restart` — plus terminal states.
The watchdog, drain, and health surface all read these states instead of
guessing. (Server invariant: every running turn has exactly one of: a live
handle, a parked record, or a terminal status.)

### 2.2 Graceful restart: drain, park, notify — never replay unrequested ⚑

One drain-gated `restart_this_worker` that ALL restart paths route through
(Settings, platform Restart-to-finish, deploy, sibling scripts):

1. `draining` set → new sends append to the durable queue ("queued —
   applying update") instead of starting turns.
2. **Drain** each live turn through a new `DrainForRestart` path — NOT
   `stop_chat_for` (Stop intentionally collapses/clears the queue; restart
   must never touch it) ⚑ — interrupt → runner completes → finalize persists
   the accumulated partial blocks + a one-line "paused for a platform
   update" note. Distinct `TerminalDisposition` for restart-drain. The
   restart utility's SIGKILL timer is extended to `DRAIN_TIMEOUT + grace`
   for drain-aware restarts (the 5s hard-kill stays as the crash floor). ⚑
3. Turns that can't drain in time park as `parked_for_restart` (durable
   marker; partials persisted).
4. **Boot**: reconcile finalizes anything left, then **push-notifies**:
   "Your turn was paused for an update — tap to resume." Resume is ONE tap
   (re-sends the preserved context as a continue turn). No automatic replay:
   provider resume semantics differ (Claude local transcript vs Codex
   thread), a stale marker must never resurrect an old turn, and a turn the
   owner thought finished must not spring back unrequested. ⚑
5. Queued messages: preserved (existing behavior) and promoted on the next
   owner action, exactly as today.

The 16-block class becomes, at worst: "paused for update → one tap →
continues with partials intact."

### 2.3 Liveness watchdog: event staleness on the right states ⚑

- `ChatBroadcast.publish()` stamps `last_event_at` (single choke point all
  runner/tool/task events pass through).
- One lifespan sweep (mirrors the wedged-marker sweeper): a turn in
  `streaming`/`tool_running` with `now - last_event_at > ~10 min` →
  interrupt via the normal clean path → finalize partials + a clear error
  with one-tap Resume. `waiting_for_user`, `provider_parked`, and `draining`
  are exempt (legitimately silent); the watchdog is suppressed during drain
  so it can't race the drain's own interrupt. ⚑
- Long quiet tool calls are safe: tool/task/thinking events reset the clock;
  the observed wedge was silent >1h and would be caught in 10 minutes.

### 2.4 Provider limits: park + notify at reset; resume is a tap ⚑

- Parse the reset time (structured rate-limit events where available; text
  "resets 1:40am" otherwise; fallback 30-min re-check).
- Park as `provider_parked` on the run record (`parked_until`,
  `park_reason`). The persisted block becomes a live card: "Rate limit —
  resets at 1:40am · **Resume now**".
- At `parked_until`, **push-notify**: "Your limit has reset — tap to resume."
  Default is notify + one-tap resume (the deliberate no-limit-storm boundary
  stays); **auto-resume is an owner setting, off by default**, and even then
  strictly serial with re-park-on-re-hit. ⚑

### 2.5 The floor: never-blank, canary, health

- **Named invariant (tested)**: the chat surface always shows live stream OR
  catch-up replay OR resolved transcript OR an explicit state card — never
  blank. Closed by 1.3 (no mid-stream reload, SW on a leash) + 1.2 (no
  chunk 404) + existing reconcile.
- **Post-deploy canary**: after health+ready, deploy fires one throwaway
  "reply ok" SDK turn; failure (auth/limit/wedge) degrades health +
  push-notifies. The manual ping-test, automated. Flag-gated.
- **Health surface**: `/api/debug/status` grows per-run `state`,
  `last_event_age`, `run_age`, `subscribers`, `parked_until`, derived
  `stale` — the same signals the watchdog consumes. The observed wedge
  lights up instantly.

### 2.6 Intelligence vs substrate

Substrate (built, ironclad): everything above — identical-every-time.
Intelligence (instructed): when to `shell_apply_now`; when a backend change
warrants prompting a restart; platform-conflict resolution (existing agent
chat); whether resumed queued work is still relevant (the resuming agent
re-reads and adapts). Reflection may audit chat health and file notes — an
audit, never the load-bearing net.

## Build order (each independently shippable)

1. **Generation serving: attic (+index) + request-time resolution +
   asset-404** — L. Kills the visual-breakage class; makes everything else
   safe. (Pairs naturally with the card-189 shell-legacy removal.)
2. **Client apply-on-idle + SW waiting-lifecycle migration** — L. Kills
   disruptive reloads; the SW half is load-bearing, not optional. ⚑
3. **Run-states + `last_event_at` + liveness watchdog** — M. Kills the
   wedge class; the states also serve items 4–6. ⚑
4. **Drain-gated restart (`DrainForRestart`, kill-timer, park, boot
   notify + one-tap resume)** — L. Kills the 16-block class.
5. **Warm-build/publish split (`vite build --watch` + settle/apply-now
   publication)** — M/L. The speed win, and closes the SW install race. ⚑
6. **Provider-limit parking + reset-notify + opt-in auto-resume** — M.
   Kills the 15-block class.
7. **Canary + health surface** — S.
8. **`shell_apply_now` + seed line** — S.

## Trade-offs accepted

- Watch-mode ≈1–3s + publish-on-settle, not HMR-instant; serving stays
  unified. HMR is the named future tier, adopted deliberately or not at all.
- Drain adds ≤20–30s to restarts (owner-initiated, infrequent).
- Attic keeps K=3 generations of assets + index (hardlinked, pruned,
  gitignored).
- Resumption is one tap, not automatic — a deliberate explicit boundary;
  push notifications make it a 2-second action.
- Limit-reset parsing is string-fragile → lenient 30-min fallback (degrades
  to "notified late", never "never notified").
