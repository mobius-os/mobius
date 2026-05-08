# Möbius backlog

Running list of user-reported items and ideas, ordered roughly by
priority within each section. Update as items ship or get re-scoped.

## In progress

_(none right now)_

## Next up — UX polish

- **Drawer chat list / apps list still imperative.** Migrate to
  `useChats` / `useApps` queries (the foundation is in place; this
  is the mechanical follow-up to the TanStack work).
- **Spacer test 9 flake under Playwright chromium.** "Short response
  — spacer preserved after reload" times out in the full chromium
  suite ~1 in 2 runs but passes alone. Mitigated in 81170a7 by
  bumping its per-test timeout 8s → 15s; if it still flakes after
  that, either bump retries to 1 in `playwright.config.js` or split
  the suite into smaller projects.

## Next up — agent / iteration loop

- **Run Session 13 with the new prompt mix** (vague →
  trivial-then-escalate → directive). Was added to the demo playbook
  but never executed.
- **Read prod chat logs since Session 12** and upstream learnings
  into the seed. Specifically: the ISS chat where the agent figured
  out texture caching / smoothing — there's reusable knowledge there.
- **Speed up agent startup tool-calls.** The agent often does ~5
  Read/grep calls before producing visible work. Investigate which
  are necessary, whether the experience file already documents what
  it's looking for, and whether the agent can parallelize the
  remaining reads. Goal: reduce time-to-first-meaningful-action
  WITHOUT letting the agent skip its clarify gate.
- **Screenshot viewport mismatch.** The screenshots the agent takes
  via agent-browser appear smaller than the user's actual phone
  viewport. Either the agent is using a default viewport (likely
  ~360×640 fallback) instead of the partner's `Viewport: WxH` from
  context, or `agent-browser set viewport` isn't being called.
  Check the seed's screenshot section + verify on a real run.

## Future — architecture

- **`chat_updated` SSE event** for server-driven cache invalidation
  when chats are mutated outside the streaming path (e.g. recovery,
  backend-only edits).
- **Owner-status `useQuery`** to replace the imperative fetch in
  `App.jsx`.

## Closed (recent)

- (81170a7) App iframe LRU + token-free postMessage protocol +
  chat hide-then-reveal scroll restore + nav back-target sentinel
  fix. Multi-iframe LRU (cap 4) keeps recently-visited apps mounted
  with preserved state; render order is sorted by id (NOT LRU
  recency) so React never reorders keyed wrappers — DOM reparent
  was reloading sandboxed iframes and tripping the 10s frame-init
  timeout. Chat scroll restore moved to a hide-then-reveal pattern
  (`visibility: hidden` until RO-settle + safety cap) — kills the
  visible jitter without racing the browser to set scrollTop
  before paint. Multi-mount chat-cache experiment was tried and
  reverted; structural cost (sessionStorage flush gap, stale
  closure on applyScroll, native scrollTop reset on reparent) was
  far higher than user-visible benefit. `navTo` now ensures a
  back-target sentinel exists so deep-link → "Open app" →
  swipe-back returns to chat instead of exiting the PWA.
- (74d3800) Navigation hardening: flushSync(setDrawerOpen(false))
  before pushState in navTo + Shell.newChat. CLAUDE.md "Navigation
  non-negotiable constraints" section locks 6 rules. Test 21
  "BFCache snapshot contract" Playwright guard. Seed audit removed
  3 over-fitted gotchas + added the frequency × cost philosophy
  for what goes in the seed vs the running experience file.
- (5388561) App-back-refresh fix: app token cached via TanStack
  Query, module URL version-busted, SW cache-first for module URLs.
- (84ca8b6) Drawer rewrite: purely visual state, navigation pushes /
  pops history. Eliminated the +1 history leak and the "back closes
  drawer first" half-step.
- (bed070b) Back-nav jitter + send-flash fix.
- (cc1cbe3) TanStack Query data layer + IndexedDB persistence.
- (eb368f9) Service-worker timeout, vendored three.js, Claude CLI
  bump, cron PATH fix, scroll guard.

## Reading list

Things the user has linked / referenced and would like incorporated
where applicable:

- https://slicker.me/webdev/pwas-offline-first.html — applied: SW
  network-first timeout, persistent storage request, IndexedDB
  cache for chat queries. Deferred: full local-first / IndexedDB
  primary store / CRDTs (overkill for single-owner Möbius).
