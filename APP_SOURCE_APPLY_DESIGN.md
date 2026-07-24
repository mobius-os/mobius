# Explicit mini-app source application

Status: implemented; release verification is in progress.

## Decision

Replace the mini-app source watcher's hidden apply/commit/update-resolution
state machine with two explicit operations:

1. `apply_app.py <source-dir>` accepts an ordinary source revision and makes it
   live.
2. `resolve_app_update.py <source-dir>` finalizes an owner-approved Store
   conflict resolution through the existing installer.

An agent calls the appropriate operation after a coherent edit. A failed call
leaves the previously published app live and can be retried. There is no
background mini-app source watcher.

The Store installer remains the authority for installs and upstream updates.
Screenshots, interaction testing, cron execution, and contribution submission
remain separate operations.

## Why change

`backend/app/app_watcher.py` currently does much more than observe files. A
source event can:

- find an app row;
- synchronize local manifest authority;
- compile and publish a bundle;
- commit Git source;
- infer that conflict markers were resolved;
- finalize a Git merge;
- replay a pending Store installation;
- publish owner-visible events; and
- retry pending update work after process restart.

Those are multiple workflows with different authorization, recovery, and
transaction boundaries. Triggering them from a polling event makes the
platform infer intent from timing and file contents. It also creates two
writers for Git: the explicit registration helper and the watcher.

This conflicts with the repository's stated architecture principle: automate
the deterministic operation, instruct the agent to invoke it, and leave
ambiguous recovery to the agent.

## Goals

- One explicit authority accepts a local source revision.
- The live bundle, stored entry source, local manifest contract, and accepted
  Git commit describe the same revision.
- A compile, validation, Git, or database failure leaves the previous live
  revision intact.
- Retrying the same source is safe and does not create empty commits.
- Multiple file edits can be applied as one revision.
- Store conflict resolution is explicit and remains capability-review safe.
- Agents receive a useful synchronous error instead of relying on background
  logs or a transient toast.
- Direct source saves do no hidden work.
- The implementation removes more state-machine code than it adds.

## Non-goals

- Do not combine visual verification with source application.
- Do not replace the Store install/update transaction.
- Do not turn app application into a job queue.
- Do not add a database-backed apply workflow or progress state.
- Do not provide HMR for half-written source.
- Do not preserve automatic recompilation after an unaccompanied manual edit.
  A person using a terminal runs the same explicit command as an agent.
- Do not make local manifest edits grant reviewed Store capabilities.

## Current writers and their future authority

| Current path | Current behavior | Future authority |
|---|---|---|
| `register_app.py` | POST/PATCH, then Git commit | Replaced by `apply_app.py` |
| `POST /api/apps/` | Creates row and compiles inline JSX | Removed after callers migrate |
| `PATCH /api/apps/{id}` with JSX | Compiles inline JSX | Removed; PATCH remains metadata-only |
| `app_watcher.py` ordinary edit | Compiles, commits, publishes | Removed |
| `app_watcher.py` resolved Store conflict | Commits and replays installer | Replaced by explicit resolve operation |
| `install_from_manifest` | Installs/merges/compiles/commits Store source | Unchanged authority |
| Boot bundle reconciliation | Rebuilds missing/old artifacts from DB source | Remains a bundle-recovery operation; never commits source |
| Metadata-only PATCH | Renames, pins, revokes permissions | Remains metadata-only |
| GitHub contribution routes | Read/stage/submit source repositories | Unchanged; they do not publish app source |

All source-publishing API entry points must call the same domain service.
Keeping HTTP routes is acceptable; duplicating the transition behind them is
not.

## Source of truth

For a local or locally modified app:

- The app directory is the editable draft.
- The per-app Git `main` commit is the accepted source revision.
- `App.jsx_source` mirrors `index.jsx` from that accepted revision.
- `App.compiled_path` names the immutable bundle compiled from that revision.
- The local app's normalized capabilities and `offline_capable` value come
  from `mobius.json` in that revision.

For a Store app:

- `upstream` remains the reviewed pristine source.
- `main` remains the accepted local result.
- Capabilities continue to come only from the reviewed Store manifest.
- An ordinary local source apply may change code but must not change reviewed
  capabilities.
- A pending upstream conflict can only advance through
  `resolve_app_update.py`, which re-enters `install_from_manifest`.

## Operations

### Apply ordinary source

Agent interface:

```bash
python "$SCRIPTS_DIR/apply_app.py" /data/apps/<slug>
```

The helper:

1. Resolves the requested source directory.
2. Sends only that directory and the owning chat id.
3. Prints the resulting app JSON and exits nonzero on any failure.

The server, under lifecycle → app → source-dir locks:

1. Revalidates source-dir confinement and row identity.
2. Rejects a pending Store update; that requires the resolve operation.
3. Stages the working tree into a temporary Git index and writes an immutable
   candidate tree without changing the app repository's real index.
4. Materializes that candidate tree into a temporary directory.
5. Validates `mobius.json` and compiles `index.jsx` from that directory.
6. Stages the live working tree into a second temporary index and rejects the
   apply if its tree differs from the candidate.
7. Commits the exact candidate tree with an expected-parent compare-and-swap.
8. Publishes the immutable bundle and commits the App row.
9. Removes the previous unreferenced bundle.
10. Publishes `app_created` or `app_updated`.

The helper does not parse app identity, send inline source, or run Git itself.
Git and publication are one server-owned transition.

For a new local app, `mobius.json.id` must equal the source-directory basename.
Its `name` and `description` populate the row. For an existing local app, those
two manifest fields update the corresponding display metadata in the same
transaction. A Store app keeps its reviewed identity and metadata during an
ordinary local-code apply.

The complete manifest shape is validated, but a local apply intentionally
projects only owner-controlled local runtime fields: `offline_capable` and
host-mediated `capabilities`. It does not grant Store-reviewed server
permissions, install skills, seed storage, register schedules, fetch static
assets, or change install identity. Those remain installer operations.

### Resolve a Store update

Agent interface:

```bash
python "$SCRIPTS_DIR/resolve_app_update.py" /data/apps/<slug>
```

The server:

1. Requires a Store app with a matching pending-update receipt.
2. Requires an in-progress materialized merge for that receipt.
3. Refuses unresolved text markers, unmerged paths, and unresolved binaries.
4. Commits the resolved source as the existing single-parent replay.
5. Calls `install_from_manifest` with the receipt's expected app, upstream
   commit, capability digest, and candidate digest.
6. Clears the receipt only when installer promotion succeeds.
7. Publishes `app_updated`.

The operation is idempotent. If the resolution commit exists but installer
promotion was interrupted, the same command skips the already-complete Git
step and retries the matching receipt.

There is no boot-time replay. The durable receipt remains visible as
retryable work, and the resolver chat instructs the agent to retry explicitly.

## Stable snapshot without a new daemon

Raw filesystem writers do not participate in the server's asyncio locks. The
apply transition therefore cannot assume that holding `source_dir_lock`
freezes source bytes.

The implementation uses Git's tree and ignore semantics rather than a second
custom source-file inventory:

1. Ensure the per-app repository and managed ignore rules exist.
2. Create a temporary Git index seeded from `main`.
3. Stage the working tree into it with the same pathspec used by app Git and
   write candidate tree `T1`.
4. Materialize `T1` into a temporary compile directory. Git modes are
   preserved; a tracked symlink is materialized as a plain file containing its
   link text, matching the installer's untrusted-source checkout policy.
5. Validate and compile that immutable directory.
6. Repeat temporary-index staging to produce `T2`.
7. If `T1 != T2`, discard the staged bundle and return
   `409 source_changed`; the caller retries after its edit settles.
8. Commit `T1` directly, parented on the `main` tip captured at the start, and
   update `main` only if that expected tip is still current.

If a new edit lands after the commit, it remains a dirty draft for the next
explicit apply. It cannot silently enter the bundle that was just published.

This is intentionally optimistic. It does not add leases, edit sessions,
write proxies, or a queue. A stable editing burst succeeds once; overlapping
writers receive a retry instead of publishing mixed revisions.

The real Git index is never used as scratch state. A failed validation or
compile therefore cannot leave paths staged, mark an unresolved path resolved,
or disturb a person's explicit Git staging.

## State transitions

### Local source

| State | Event | Next state | Live app |
|---|---|---|---|
| Applied | File edit | Draft | Previous revision |
| Draft | Explicit apply begins | Applying | Previous revision |
| Applying | Validation/compile/Git failure | Draft | Previous revision |
| Applying | Source changes during snapshot | Draft | Previous revision |
| Applying | Git succeeds, DB commit fails | Accepted-ahead | Previous revision |
| Accepted-ahead | Retry same apply | Applied | Accepted revision |
| Applying | Git + DB publication succeed | Applied | New revision |
| Applied | Reapply unchanged source | Applied | Same revision; no empty commit |

`Accepted-ahead` is not a new stored status. It is the recoverable condition
where Git `main` already contains the source but the App row still references
the previous bundle. The next idempotent apply compiles that same tree and
advances the row.

### Store conflict

| State | Event | Next state | Live app |
|---|---|---|---|
| Installed | Reviewed update conflicts | Pending | Previous revision |
| Pending | Owner opens resolver | Resolving | Previous revision |
| Resolving | Agent saves partial resolution | Resolving | Previous revision |
| Resolving | Explicit resolve finds conflicts | Resolving | Previous revision |
| Resolving | Resolution commit succeeds | Resolved-pending-promotion | Previous revision |
| Resolved-pending-promotion | Installer replay fails | Same state | Previous revision |
| Resolved-pending-promotion | Explicit retry succeeds | Installed | New revision |

The pending receipt and Git refs already represent these states. No parallel
database status is added.

## Transaction and crash rules

| Failure point | Durable result | Retry behavior |
|---|---|---|
| Before snapshot | Draft only | Apply again |
| During compile | Draft only; staging removed | Fix source and apply |
| During Git commit | Draft or accepted commit; old bundle live | Apply again |
| After Git commit, before bundle publication | Accepted Git commit; old bundle live | Apply same revision |
| After bundle publication, before DB commit | Orphan immutable bundle; old row live | Boot reaper removes orphan; apply again |
| After DB commit, before old-bundle cleanup | New row live; old bundle orphaned | Boot reaper removes old bundle |
| After DB commit, before broadcast | New row live | Shell refetch/reconnect sees durable row; apply may be retried |
| Resolve commit before installer promotion | Receipt + resolved Git commit; old app live | Resolve command retries installer |

The database and Git cannot share one atomic transaction. The ordering is
chosen so every crash exposes either the previous live app or the fully
published new app. Git may be ahead temporarily; the live bundle is never
ahead of accepted Git source.

## Locking

Preserve the documented acquisition order:

```text
install_uninstall_lock → app_storage_lock(app_id) → source_dir_lock(source_dir)
```

- Creation has no visible app id until flush; it holds lifecycle and source-dir
  ownership while allocating the row and compiling. It does not acquire an app
  lock for that not-yet-visible id.
- Update reloads the row under lifecycle/app locks before taking source-dir
  lock.
- Store resolution releases inner app/source locks before re-entering the
  canonical installer where required, as the current installer does.
- No function silently acquires an outer lock from inside an inner lock.

The design does not claim that an asyncio source lock blocks raw file writes;
the repeated temporary-index tree comparison closes that gap.

## API shape

Add source-specific endpoints rather than overloading metadata PATCH:

```text
POST /api/apps/apply
POST /api/apps/resolve-update
```

`POST /api/apps/apply` accepts `{source_dir, chat_id}`. Exact resolved
`source_dir` is the stable identity: the operation creates when no live row
owns it and updates when one does. The response says whether it created,
updated, or performed an unchanged idempotent apply. There is no preliminary
list/read and no separate create-vs-update decision in the helper.

`POST /api/apps/resolve-update` likewise accepts `{source_dir}`. The durable
pending receipt supplies the reviewed app id and candidate identity; the helper
does not list apps merely to translate the directory back to an integer id.

`PATCH /api/apps/{id}` becomes metadata-only. `source_dir` is immutable after
creation. `POST /api/apps/` and inline `jsx_source` mutation are removed after
internal tests and callers migrate. There is no compatibility shim.

The Store install endpoint remains separate because it carries reviewed
manifest/source digests and remote acquisition semantics.

## Events and user experience

- Successful create emits `app_created`.
- Successful update or resolved Store promotion emits `app_updated`.
- A failed apply returns the error synchronously to the agent and emits no
  success event.
- The previous compiled bundle remains served after a failed apply.
- The shell does not need watcher-specific behavior.
- “Save to preview” becomes “apply to preview”; the agent normally applies
  once for the first useful slice and once after meaningful polish, not after
  every keystroke.

## Removal

Delete:

- `backend/app/app_watcher.py`;
- watcher startup/shutdown wiring in `main.py`;
- Watchdog dependency if the frontend watcher no longer requires it (it
  currently does, so retain the package);
- watcher-specific tests and watcher-specific skill instructions;
- register/apply Git ownership split;
- boot-time pending-update watcher replay.

Keep:

- `frontend_watcher.py`; it is a separate platform-source build system and is
  out of scope;
- immutable bundle publication and boot reapers;
- per-app Git merge primitives;
- Store installer rollback;
- system event delivery.

## Implementation sequence

1. Add temporary-index Git-tree helpers with focused tests.
2. Add the canonical local apply service.
3. Add the single apply endpoint and `apply_app.py`.
4. Route source-publishing tests and internal callers through apply.
5. Add explicit Store resolution endpoint and helper.
6. Update the resolver and building skills.
7. Remove the mini-app watcher and lifecycle wiring.
8. Remove inline source mutation from generic create/PATCH schemas and migrate
   callers.
9. Run broad backend, frontend, E2E, and live agent benchmarks.
10. Update `ARCHITECTURE.md`.

Each step must leave one source-publishing authority. Do not temporarily add a
second watcher-like reconciler.

## Required tests

### Local apply

- Create from a valid source directory.
- Update a multi-file app and commit all source once.
- Invalid manifest leaves prior bundle and row unchanged.
- Compile failure leaves prior bundle and row unchanged.
- Git failure leaves prior bundle and row unchanged.
- Database failure after Git commit is retryable.
- Reapplying unchanged source succeeds without an empty commit.
- A source change during snapshot returns `409` and publishes nothing.
- A source change after accepted commit remains a dirty, unapplied draft.
- Local manifest capability changes land with the compiled revision.
- Store app local edits do not alter reviewed capabilities.
- Event fires only after durable publication.

### Store resolution

- Partial text resolution is rejected.
- Unresolved binary conflict is rejected.
- Valid resolution promotes through installer.
- Network failure after resolution commit leaves receipt retryable.
- Retry after restart succeeds without a watcher.
- Changed candidate digest requires renewed review.

### Removal

- Process startup has no mini-app source observer.
- Editing a file alone does not change `compiled_path` or `updated_at`.
- Explicit apply updates the open iframe.
- No test waits for a watcher debounce.

### End to end

- First useful app is applied and visible early.
- A second edit remains invisible until apply, then refreshes once.
- Invalid draft keeps the prior version running.
- Visual verification still enters the opaque iframe by ref.
- App repository is clean after successful apply.

## Rejected alternatives

### Keep the watcher but remove Git commits

This still infers publication intent from a file event and leaves conflict
resolution hidden. It reduces one race but keeps the wrong authority.

### Keep a thin watcher that automatically calls apply

This is an optional future developer convenience, not the product lifecycle.
Shipping it now would preserve “save means hidden apply” and retain timing as
intent. Remove the watcher first and measure whether manual-edit ergonomics
actually require it.

### One `finalize_app` command that also validates UI and screenshots

Source acceptance is deterministic. Visual quality and interaction are
reasoning tasks with different failure semantics. Combining them would make a
compile retry rerun browser work and would obscure which state is durable.

### Durable apply jobs

Local compilation is bounded and already request-sized. A job table, worker,
lease, and progress protocol add recovery states without evidence that the
synchronous operation needs them.

### Filesystem lease or editor proxy

The agent already edits ordinary files. Requiring every write through a custom
proxy would make common edits slower and create another tool protocol.
Optimistic snapshot verification prevents mixed publication with far less
machinery.

### Custom file enumeration and byte fingerprinting

This duplicated Git's ignore, path, executable-bit, and symlink rules. Any
future change to the tracked-source contract could make the compiler snapshot
and accepted commit disagree. A temporary Git index gives one canonical tree
without mutating the real index.

### Publish the working tree and commit later

This is the current ownership problem in another order. A crash can leave live
code with no accepted Git revision. Git acceptance must precede DB publication.

## Design review checklist

- [x] Every source mutation path is mapped to one authority.
- [x] No success depends on a polling event.
- [x] No local apply can grant reviewed Store capabilities.
- [x] Live code always corresponds to accepted Git source.
- [x] Failures keep the prior bundle live.
- [x] Retry is idempotent at every crash boundary.
- [x] Lock acquisition follows the global order.
- [x] Raw file writes are not falsely assumed to honor asyncio locks.
- [x] Conflict resolution cannot silently accept a changed upstream candidate.
- [x] No new queue, daemon, compatibility layer, or duplicate state is added.
- [x] Skills describe the explicit operations without duplicating tool internals.
- [x] Benchmarks measure first apply, first real screenshot, completion, tools,
      and cached/uncached tokens.

## Adversarial review findings

The review changed the proposed design in six material ways:

1. **One apply endpoint, not create and update variants.** The exact source
   directory already supplies stable identity. A second endpoint and a
   list-before-write helper would add latency and divergent branching.
2. **Git tree, not a custom fingerprint format.** The initial design repeated
   Git's source inventory and still had a race between compiling a copy and
   committing the live tree. Temporary indexes produce an immutable candidate;
   both compile and commit consume that exact tree.
3. **No real-index mutation.** A failed compile must not stage files or
   accidentally advance conflict-resolution state.
4. **Explicit metadata authority.** A local manifest may update local display
   metadata and runtime declarations, but an ordinary Store-app edit cannot
   change reviewed identity, permissions, skills, static assets, seeds,
   schedules, or capabilities.
5. **Creation identity is deterministic.** Requiring the manifest id to match
   the directory basename avoids slug allocation, name matching, and a hidden
   source-dir rename during the first apply.
6. **Restart recovery stays durable but explicit.** Pending Store receipts and
   Git refs survive a restart. The resolver command can retry without a
   watcher; the UI must continue to expose pending resolution, and no boot task
   silently promotes it.

The remaining irreducible boundary is Git versus SQLite: they cannot commit
atomically. The accepted-ahead state is therefore deliberate, derived from
existing durable Git state, and requires no new status column or reconciler.
