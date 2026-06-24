# Outer-loop infra designs

Design plans for improving the HOST-SIDE dev/test/deploy loop (NOT in-product
features). Produced from the 2026-06-24 infra review (43 adversarially-verified
findings) + prod-chat signals, then each design was adversarially reviewed again
(Codex per-card + a Claude cross-card pass) against the real code and corrected.

These live here (tracked, force-added — `docs/` is otherwise gitignored) so they
survive a host wipe; the gitignored `.pm/features/13{7,8,9}-*.md` + `140-*.md`
cards are the local backlog trackers and point here.

| Doc | Status | Pri | Summary |
|---|---|---|---|
| [137 — protect the live owner](137-protect-live-owner.md) | 1b ready · 1a/1c idea | p1 | Stop dev-loop restarts from losing the owner's messages: presence-gate (ready), durable mid-turn UX (design), remote/ephemeral staging. |
| [138 — deploy served-version truthfulness](138-deploy-served-version.md) | ready | p1 | `/api/version` reports the SERVED `/data/platform` (via a boot sentinel), assert it in the deploy verify, one shared `stamp_baked_floor()` helper. |
| [139 — wire 134's merge into the deploy path](139-platform-deploy-merge.md) | idea | p1 | A diverged prod takes a release via the shipped 134 engine, invoked in-container from the deploy under the sync lock; protected files a separate root overlay. |
| [140 — session-gc.sh reaper](140-session-gc-reaper.md) | ready | p2 | Dry-run-default GC for merged worktrees / branches / stale containers; merged-detection via ancestry AND `git cherry` (squash-aware). |

Sequencing: 137-1b + 138 are small/ready; 137-1c (staging) is the structural
lever; 137-1a (durable chat) is a true Claude+Codex ensemble in its own session;
138 + 139 share one entrypoint edit (139 is the superset); 140 is independent.
