# Möbius backlog

Running list of user-reported items and ideas, ordered roughly by
priority within each section. Update as items ship or get re-scoped.

## Design philosophy (read first)

**Code empowers the agent; it does not police the agent.** When
working on subsystems the agent touches (themes, mini-apps,
experience log, anything in `/data/shared/` or `/data/apps/`):

- Prefer well-designed defaults and clear in-code documentation
  over server-side rewriting of the agent's output.
- Make the contract discoverable from the code itself (comments on
  why a variable exists, what consumes it, what the safe range is)
  rather than spelling it out in the seed.
- Mistakes should be hard to make because the design is hackable
  with low cognitive load, not because validators block them.

Infrastructure the agent doesn't see — provider plumbing, queue
state machine, recovery atomicity, streaming protocol, shell
internals — is fair game for whatever level of complexity makes
it correct. The split is intentional: invisible robustness for
the platform, maximal expressive surface for the agent.

When in doubt: would documenting this in code/seed cost less than
maintaining a sanitizer? Usually yes. Reach for the sanitizer only
when a mistake is silent + catastrophic + indistinguishable from
intentional design.

## In progress

_(none right now)_

## Next up — agent contract / discoverability

(Both items align with the "code empowers the agent" philosophy:
the current contracts surprise agents in non-obvious ways and the
fix is to make the design itself less surprising.)

- **Symmetric storage API.** `PUT /api/storage/apps/{id}/file.json`
  accepts `{content: JSON.stringify(myData)}` while `GET` returns
  the parsed inner object — the asymmetry has cost agents at least
  two compile cycles per build (per the chat-2 introspection). Fix:
  let `PUT` accept the inner object directly; the server stringifies.
  Keep `{content: "..."}` as a legacy shim for existing mini-apps,
  with a deprecation log line. Read shape stays as-is (already the
  good shape).
- **CSS contract comments in Shell.css + ChatView.css.** The
  Andalusian-theme failure traced to: agent knew variable names
  from `theme.py:DEFAULT_THEME` but didn't know which selector
  paints which variable. Fix: add `/* CONTRACT: ... */` comments
  beside each `background: var(--bg)` / `var(--surface)` / etc. in
  Shell.css and ChatView.css, explaining what relies on this being
  opaque/solid. Comments are greppable, live next to the rule, and
  hard to miss in normal editing. (A generated `theme-selectors.json`
  catalog was considered and rejected — comments first; add the JSON
  only if multiple agents still trip after the comments land.)

## Next up — agent UX

- **Notify on pending input.** When the agent leaves a turn open
  waiting for the partner (AskUserQuestion card with no answer yet,
  or prose clarifying questions that block building), it should
  send a push notification so the partner sees it even if the tab
  is in the background. Surfaced after a live demo where the agent
  asked theme questions, the partner switched apps, and didn't see
  the prompt until much later. Seed should remind the agent to fire
  `notifications/send` whenever a turn ends without delivering work;
  today notifications are only the post-build courtesy.

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
