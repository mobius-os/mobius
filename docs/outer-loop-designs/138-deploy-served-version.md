# 138 — Deploy truthfulness: report the SERVED platform version

Status: READY (after the review corrections below)  ·  Priority: p1
Backlog tracker: `.pm/features/138-deploy-served-version.md`
Adversarially reviewed (Codex + cross-card, 2026-06-24); `[review]` marks folded
corrections.

## Why

`/api/version` reports the IMAGE `BUILD_SHA` (`main.py:463`, `config.py:35,39`;
unauthenticated), not the served `/data/platform` code. The served backend is
`/data/platform/app` (symlinked over `/app/app`), which persists across image
deploys — so "deployed" is structurally false-green when `/data/platform`
diverges or step-3b skips the sync. During the 134 deploy, `/api/version` read
`7083b9f` while the served HEAD was `4803c50` until I reconciled by hand.

## What

### 1. Report the served source — via a boot SENTINEL, not blind `git HEAD` `[review]`

The entrypoint already chooses which source it serves (`/data/platform` vs the
baked `/app/app` fallback when the platform tree is missing/broken). A blind
`git -C /data/platform rev-parse HEAD` is WRONG in the baked-fallback case
(it would report a platform HEAD that isn't being served). Fix:
- Entrypoint writes a boot sentinel, e.g. `/tmp/serving-source` = `platform`|`baked`,
  at the point it resolves the symlink.
- `/api/version` adds `{serving_source, platform_sha, platform_dirty, baked_sha}`.
  Read `git -C /data/platform rev-parse HEAD` ONLY when `serving_source=platform`.
  uvicorn runs as `mobius` (`entrypoint.sh:900`), so the read is dubious-ownership
  safe. Never raises (degrade to `unknown`). Leaking a short sha + dirty bool is
  fine (the image sha already leaks via BUILD_SHA) `[review]`.
- `platform_dirty` MUST filter `.baked-sha` (it's tracked-but-churns) using the
  same filter step-3b uses (`git status --porcelain | grep -vE '^.{2} \.baked-sha$'`)
  or gitignore `.baked-sha` inside `/data/platform` — otherwise it reads
  permanently dirty `[review]`.

### 2. Assert the SERVED code in the deploy verify — reuse step-3b, don't re-derive `[review]`

The shipped step-3b fix (`f18d22f`) detects DIVERGENCE; it does not assert
served==baked. Add an explicit verify step AFTER the post-3b restart + readiness
checks, BEFORE the existing image-SHA verify:
- On the clean path (step-3b ran `recovery_restore.sh platform-baked`), assert
  `git -C /data/platform rev-parse HEAD` == the new `restore: platform-baked`
  commit (and its tree == the baked floor by the content `diff -rq` already used).
- On the diverged path, the verify must print plainly "served backend NOT updated
  (platform diverged)" rather than green-checking the image SHA.

### 3. Stamp the baked floor — ONE shared helper across all writers `[review]`

`recovery_restore.sh platform-baked` (`recovery_restore.sh:79-131`) copies +
commits but never writes `.baked-sha` or tags `baked-<sha>` — confirmed real;
that's why status reads "available" right after a clean deploy (I stamped it by
hand for 134). The entrypoint upgrade-notice block (`entrypoint.sh:336-356`)
advances `.baked-sha` WITHOUT copying code, and the entrypoint crash-loop
auto-restore (`entrypoint.sh:74-95`) restores baked without stamping either.
Fix: a single `stamp_baked_floor()` helper (writes `.baked-sha` = BUILD_SHA,
force-tags `baked-<sha>` at HEAD) invoked by ALL writers: recovery_restore
platform-baked, entrypoint init/upgrade, the crash-loop restore — and 139's
deploy-merge commit. Run the stamp as `mobius` (wrap in `su -s /bin/sh mobius`
inside the root recovery_restore call, or chown `.baked-sha` + the tag ref to
`mobius:mobius` immediately) so the stamp isn't root-owned `[review]`.

### 4. Close the clean-check→restore race `[review]`

Step-3b checks "clean" then later runs the root restore — a window where the
agent could commit between. Re-validate clean INSIDE the same atomic root
operation (a root-side script that re-checks HEAD + porcelain and aborts if
changed, under the platform sync lock).

## Coordination
- **138 and 139 edit the SAME entrypoint upgrade block + the `.baked-sha` stamp.**
  Sequence: 139 is the superset entrypoint edit (seed/advance `upstream` + stamp);
  fold 138's stamp-fix into the shared `stamp_baked_floor()` helper so they don't
  collide `[review]`.

## Done when
- [ ] entrypoint writes `/tmp/serving-source`; `/api/version` reports
      `{serving_source, platform_sha, platform_dirty(filtered), baked_sha}`, never raising.
- [ ] deploy verify asserts served==baked-restore-commit on clean; prints
      "served NOT updated" on diverged.
- [ ] `stamp_baked_floor()` shared helper, mobius-owned, called by all baked writers.
- [ ] clean-check→restore re-validated atomically under the sync lock.
- [ ] A deploy onto a diverged /data/platform no longer reports success while
      serving stale code.
