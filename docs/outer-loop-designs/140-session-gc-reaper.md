# 140 — session-gc.sh: report-by-default reaper for dev debris

Status: READY (after the review corrections below)  ·  Priority: p2
Backlog tracker: `.pm/features/140-session-gc-reaper.md`
Adversarially reviewed (Codex + cross-card, 2026-06-24); `[review]` = folded.

## Why

`git worktree list` shows ~93 worktrees (many merged), ~100 local branches, and
test containers "Up 12 days" on a memory-tight host. No collector exists.
`scripts/git-doctor.sh` exists but only repairs `core.bare`/pollution — distinct
scope `[review]`. session-gc is net-new.

## What — dry-run-default, advisory, `--fix` to act

### Discovery `[review]`
Enumerate from `git worktree list --porcelain` (the canonical source) and
classify by path prefix — do NOT hard-code only `.claude/` + `/tmp/` (that misses
`.codex/` and sibling-dir worktrees).

### Classification (the review demolished the first draft's safety model)

The first draft claimed "structurally incapable of touching live work" via a
`clean + ancestor-of-main + unlocked` triple. **Two of those are false:**

- **"unlocked" means nothing here `[review]`.** `mobius-session.sh` never calls
  `git worktree lock`; only 1 of 93 worktrees is locked. Drop the "structurally
  incapable" claim. Real safety = clean + correct merged-detection + a per-path
  re-check immediately before each `--fix` removal (TOCTOU) + dry-run default.
  (Optionally: add real `git worktree lock` leases to `mobius-session.sh` so an
  active session is detectable — a separate improvement.)
- **Ancestor-of-main MISSES squash/cherry-pick-landed branches `[review]`.** The
  repo lands many `session-*` branches by squash (≥12 today where `git cherry
  origin/main <branch>` shows only `-` commits = patch-equivalent-upstream) whose
  tips are NOT ancestors of origin/main. Use TWO categories:
  - `MERGED_BY_ANCESTRY` (tip is an ancestor of origin/main) → safe to advise/auto.
  - `PATCH_EQUIVALENT_UPSTREAM` (`git cherry` all `-`) → squash-landed; advise
    only with explicit opt-in / manual confirm. Never auto-remove on ancestry alone,
    and never CLAIM ancestry catches squash merges.
- **Detached-HEAD worktrees** (the `/tmp` ones, deploy worktrees): branch checks
  don't apply → default KEEP unless an explicit `--include-detached <path>` flag
  and HEAD is verified reachable from origin/main `[review]`.

### Removal-blocker preflight `[review]`
`git status --porcelain` clean does NOT detect root-owned `__pycache__`/`dist`
(left by docker runs as root) that make `git worktree remove` fail with EACCES.
Add a filesystem preflight (`git status --porcelain --ignored=matching | grep
'^!! '` then `stat` for owner != current user) → report `NEEDS_CHOWN` (advise the
`sudo chown`, CLAUDE.md cleanup §5), not `REAPABLE`.

### Containers `[review]`
`docker inspect ExecIDs` is NOT exec history (it's in-flight execs; null for
running test containers). Use `State.StartedAt` age as the staleness signal
(distinct from `.Created`), and either drop the "recent exec" clause or derive it
from `docker events --since`. Advise `docker rm -f` + `<proj>_test_data` volume
for stale ones; NEVER auto-kill — print the command. Skip the default
`mobius-test`.

### Branches
List `session-*` branches with no worktree, split into MERGED_BY_ANCESTRY (advise
`git branch -d`, safe-merge only) vs PATCH_EQUIVALENT (advise with confirm). Never `-D`.

## Output / safety
- Per-category table: KEEP(reason) / REAPABLE / NEEDS_CHOWN / NEEDS_CONFIRM(squash).
- `--fix` acts only on REAPABLE, prints each action, and RE-CHECKS clean+merged
  immediately before each removal (TOCTOU-safe).
- Acceptance criteria compute counts at runtime — NO hard-coded numbers `[review]`.
- Operator-invoked only; keep out of any cron until proven.

## Philosophy
Dry-run + advisory aligns with "code empowers, does not police" + the no-lock
convention. The fix is correctness of the merged-detection, not a stronger gate.

## Done when
- [ ] discovery from `git worktree list --porcelain`, classify all (incl. .codex/, /tmp, detached).
- [ ] merged-detection uses ancestry AND `git cherry` patch-equivalence (two categories).
- [ ] NEEDS_CHOWN preflight for root-owned ignored files; detached default KEEP.
- [ ] containers via `State.StartedAt` age; advise-only, never auto-kill.
- [ ] `--fix` removes only clean+ancestry-merged worktrees/branches with a pre-act re-check;
      reaps the current backlog without touching a single live sibling worktree.
