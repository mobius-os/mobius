# 139 — Wire the shipped 134 merge engine into the deploy path

Status: IDEA (open: how the host deploy invokes the in-container engine)  ·  Priority: p1
Backlog tracker: `.pm/features/139-platform-deploy-merge.md`
Adversarially reviewed (Codex + cross-card, 2026-06-24); `[review]` = folded.

## Why

`deploy-prod.sh` step-3b only fast-forwards `/data/platform` from the baked floor
when the platform is CLEAN; on genuine agent divergence it WARNS and leaves the
old served backend (`deploy-prod.sh:707-737`). So a customized prod can't take a
release without staying stale or discarding agent edits. Feature 134 already
shipped the engine that solves this — wire it to the deploy path.

## Reality check `[review]`

- **The 134 engine IS on main + in the prod image** (134 = commit `7083b9f`,
  verified an ancestor of origin/main). The per-card reviewer's "engine absent"
  blocker was wrong; the cross-card check confirmed `platform_update.py` present.
- **Correct API names** (the first draft invented `merge_upstream`, which does
  not exist): `seed_upstream_if_missing` (:282), `record_baked_upstream` (:316),
  `compute_merge_tree` (:379, the verdict), `write_merged_tree_to_worktree` (:429),
  `commit_clean_merge` (:471), `_seed_point` (:208).
- **Pre-134 seeding is already handled**: `_seed_point` derives the merge base
  from the ancestor `baked-<sha>` tag or the root commit by TREE, NOT from the
  unreliable `.baked-sha` file `[review]`. So "seed on a pre-134 instance" is a
  solved sub-problem — reuse it.
- This card is **the deploy-path slice of 134**, reusing the shipped engine — not
  a fold-into-134 (134 is shipped, not an open target) and not a second engine.

## What — the concrete mechanism the first draft lacked `[review]`

The engine lives INSIDE the container; `deploy-prod.sh` runs on the HOST. So the
deploy script must INVOKE the in-container engine, not reimplement it:

1. **Seed/advance `upstream` on image bump (entrypoint, shared with 138).** On
   init + every BUILD_SHA change, the entrypoint commits the new baked floor onto
   a `/data/platform` `upstream` branch (`record_baked_upstream`) and stamps
   `baked-<sha>` via the shared `stamp_baked_floor()` helper. Branch/tag seeding
   is permission-feasible (entrypoint chowns `/data/platform` then runs git as
   mobius — `entrypoint.sh:313,321`).
2. **Merge on a diverged deploy.** Replace step-3b's diverged-warn with a
   `docker exec` into the container that runs a small CLI entrypoint around the
   engine: `compute_merge_tree` verdict → clean: `write_merged_tree_to_worktree`
   + `commit_clean_merge` + `stamp_baked_floor` + restart; conflict: keep serving
   old, leave markers/MERGE_HEAD. Run it UNDER the platform sync lock, and
   QUIESCE the platform-writing agent first (reuse 137's presence-gate) so the
   merge doesn't race a live `commit_local` `[review]`.
3. **Protected/root-owned files are a SEPARATE root overlay `[review]`.** The
   engine (mobius) can't write the root-owned recovery/core files, and the
   entrypoint does NOT re-overlay baked protected files onto a diverged tree on
   restart (`entrypoint.sh:331,350`) — so protected-file changes would stay stale
   after an in-product/deploy merge. Handle them as a distinct root-run baked
   overlay (rsync the protected paths from `/app/app-baked` as root + re-apply
   perms), OR explicitly document that protected-file changes require a clean
   step-3b sync (not a merge). Do not pretend the merge covers them.
4. **Conflict hand-off, reachable from the host `[review]`.** The in-product
   resolver `_spawn_app_conflict_chat` (`routes/apps.py:456,493`) uses the DB
   session + writer actor — NOT reachable from `deploy-prod.sh`. Options: expose
   an owner-auth `POST /api/platform/resolve-deploy-conflict` the deploy can
   trigger, OR a container-side CLI the deploy `docker exec`s, OR make
   operator-manual (`git merge` guidance) the deploy-time conflict path. Pick one
   explicitly — don't reference an unreachable helper.

## Coordination
- Shares the entrypoint upgrade block + `stamp_baked_floor()` with **138** —
  implement 139 as the superset entrypoint edit; 138's stamp-fix folds in.
- Reuses **134**'s `platform_update.py` engine verbatim.

## Done when
- [ ] entrypoint seeds/advances a `/data/platform` `upstream` branch on init +
      image bump (reusing `record_baked_upstream` + `_seed_point` + shared stamp).
- [ ] step-3b on diverged invokes the in-container engine via `docker exec` under
      the sync lock, quiescing the agent first; clean→merge+commit+restart;
      conflict→markers + a REACHABLE hand-off.
- [ ] protected-file staleness handled by a documented root overlay or an
      explicit "needs clean sync" boundary.
- [ ] a diverged mobius-test instance takes a new image release via merge with
      local edits preserved (134's acceptance, on the deploy path).
