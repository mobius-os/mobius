# Safe one-way storage upgrades

## Compatibility boundary

`compat.COMPAT_LEVEL` is the highest storage step the source understands.
The database stores its compatibility floor in a single `platform_compat`
row. An active step also supplies a floor. An absent row in an existing floor
table is damage, not level zero.

`REQUIRED_IMAGE_LEVEL` names the baked fallback capability required by the
registered source. `app.main` checks this before importing database owners;
serve-time startup checks the real image again. The Docker build verifies that
the frozen runtime capability matches the baked fallback's declared level.
Reviewed update validation can inspect a future target's level in a child
process, but boot and generic restart probes scrub that candidate override.

Preflight reads the floor and records the table set before `create_all` or
ledger migrations write anything. This distinguishes fresh databases from
damaged or partially restored ones. Too-old source refuses without mutation.
Later ledger migrations cannot touch step-owned tables; the migration guard
tests this contract against an explicit baseline in registry order.

## Preparation and activation

Each `OneWayStep` declares its legacy shape, owned tables, unit conversion,
preserved-artifact checks, activation edits and post tasks. Registration is
database-free so the image check remains before database access. The first
consumer is described in `TRANSCRIPT_STORAGE_DESIGN.md`.

PREPARING leaves legacy authority untouched. Conversion, fingerprints,
archives/quarantine and target rows commit atomically in bounded units.
Interruption resumes from durable progress. A matching fingerprint does not
excuse missing recovery copies or target artifacts after a partial restore:
the step verifies them before activation.

A final pinned pass verifies source changes outside the activation write lock.
Concurrent commits cause another pass. The short activation transaction
rechecks the connection's data version, performs the authority/schema switch,
marks the step ACTIVE and raises the floor. The writer starts only after all
steps and mapped schema checks pass.

Fresh installs create the target shape and activate without converting legacy
units. Ambiguous legacy/target shapes or unrelated mapped gaps refuse instead
of guessing. Once ACTIVE, the target remains authoritative; ordinary startup
does not silently revert it to a previous representation.

## Background completion and recovery

Post tasks commit bounded batches with durable progress, retry counts and last
errors. They reserve disk for the next batch before writes. Cleanup verifies
archives before removing redundant hot values. Purging a chat deletes all its
step-owned rows and preserved copies in that chat's lifecycle transaction,
including when connection-level foreign-key enforcement is absent.

Do not equate ACTIVE with completed background work. Diagnostics report both.
Search generation, cleanup and retirement may complete while serving; a
failed task keeps its error and resumable cursor rather than claiming success.

Host replacement checks the database floor against the exact candidate or
rollback image before boot. Healthy-but-wrong-image probes do not establish a
successful rollback. When the floor refuses the previous image, the worker
settles forward instead: it restarts the new container unchanged and reports
success only if that container serves exactly the requested release. Either
way it retires the replacement journal, which exists only to restore the
previous image; keeping it would re-fence the serving app on every later run.
Before draining for a replacement, the worker reads the floor from the
serving app's own container and refuses any image below it. Source swap
rollback uses compare-and-swap ownership: unexpected HEAD or working-tree
changes are preserved, not blindly reset.

After a one-way activation, use a capable image to repair forward. Restoring
an old database is a separate owner-approved recovery operation, never an
automatic per-table reversal. Archive retirement and rebuilding a legacy form
are not implemented by this release.

## Deployment envelope

Readiness remains false until the gate has established the new authority.
Conversion must fit the actual deployment allowance, not just a synthetic hash
rate. No early-ready bypass or maintenance mode is implemented here. Prove
the matching image's gate, cutover and rollback with disposable databases and
configured host deadlines before installing this change on live data.
