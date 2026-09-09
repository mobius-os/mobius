# Migration identity cutover — 2026-09-08

This is a dated recovery procedure, not the ongoing migration policy. See
[Permanent migration contract](MIGRATIONS.md) for new development and tests.
The converter is intentionally frozen; do not add future history translations.

`124d46f08f` renumbered published local work. `35ef5a4197` temporarily recognized
its historical IDs. The finite translation now belongs to an explicit cutover,
not every startup. Historical completions are not claims that two source bodies
are byte-identical. Necessary behavioral differences must ship as new migrations.

## Ordered deployment (also applies when restoring an older backup)

Do not deploy the alias-free runner before normalizing the database. Keep the
currently healthy alias-aware revision running until this cutover is approved.

1. Record the candidate revision and obtain a consistent SQLite backup using
   `sqlite3.Connection.backup` from a `mode=ro` live connection. Do not copy a
   live `.db` without its WAL. Keep the backup private and outside Git. Run
   `PRAGMA integrity_check` on the backup and require `ok` (this is physical
   integrity, not a zero-foreign-key-debt requirement).
2. Make a disposable working copy of that backup. Point **both** DATABASE_URL
   and DATA_DIR at the disposable workspace before importing application code;
   use an unreachable API_BASE_URL and isolated MOBIUS_APP_BASE. The retention
   body deletes derived files after its transaction, so a database-only clone
   with DATA_DIR=/data is unsafe. Inspect pending bodies before running them;
   some historical migrations also carry filesystem paths in database rows.
3. Preview the finite ledger additions:

   ```sh
   python3 backend/scripts/normalize-migration-ledger-20260908.py /private/clone.db
   ```

   Then use `--apply` **on the clone**. It inserts only absent canonical
   completion rows with the historical timestamp, in one transaction. Original
   rows, canonical timestamps, unknown ledger entries, and application data
   are retained. A second normalization must add nothing.
4. Compare application contents and schema before/after normalization; only
   the expected completion rows may change. Then inspect the target registry's
   pending migrations against the normalized ledger. Replace completed bodies
   with failing sentinels; require pending bodies to run once in registry order
   and a second pass to do nothing. Run the real clone upgrade and schema-gap
   checks, allowing only the changes owned by those pending migrations.

   At the original cutover revision, the recovered snapshot needed only 0043;
   the pre-incident snapshot also needed 0042. Those are historical results,
   not an allowlist to extend each time a future migration is appended.
5. Obtain explicit owner approval to normalize the live ledger. Refresh and
   validate the backup if deployment has been delayed; verify the live ledger
   still matches the reviewed additions. Run the same `--apply` command against
   live only after approval. Never run migration bodies in the live preflight.
6. Integrate the reviewed isolated source paths. Confirm no intervening edits
   overlap them; don't sweep other chats' work. Compile/test the settled source.
   Request a **separate exact server restart approval**, describing active-turn
   interruption. Task approval or normalization approval is not restart approval.
7. After restart, verify stateful readiness, no unexpected ledger additions,
   and the active source revision. Do not use static UI availability as proof.

If normalization fails, its transaction rolls back; the alias-aware runtime
continues to work. If source activation fails, restore the previous **source**
revision first: it accepts both the retained old rows and added canonical rows.
Do not restore a full old database merely to undo this additive ledger change;
that would discard newer partner activity. Any database restoration needs its
own owner-approved recovery plan. An old-backup restore must repeat normalization
before the exact-ID runner starts. Do not expand this mapping for future renames.

## Retention audit correction

The one approved historical-body correction is
`0031_chat_retention_orphan_repair`: owned-edge postconditions remain mandatory,
and the transaction rejects newly introduced FK violations using before/after
row identities, not counts. Pre-existing unrelated debt is not a startup veto.
The SQLite baseline and writes share BEGIN IMMEDIATE, and validation occurs
before commit and before filesystem cleanup. This body only updates/deletes rows;
its FK comparison assumes stable row identities, not a table rebuild. Do not
reuse that comparison blindly for migrations which rebuild tables or change FKs.

The committed fingerprint for this body changes deliberately. The general
append-only guard and tests remain intact: `check-schema-migrations.py --against`
a pre-correction revision must still report that this body changed. Adoption of
this reviewed correction establishes the next published baseline; there is no
permanent allowlist or bypass for arbitrary historical edits. An upstream review,
if requested, must explicitly approve that baseline change rather than claim the
old-base append-only check passes. Run Contribute's **Run GitHub checks** before
**Send PR** for any separately approved contribution.

## Small regression set

- Actual deployed ledger snapshot, including old unrelated IDs and partial
  failed-startup completions; pre-incident rows are selected by their original
  timestamp rather than generated from the current registry.
- Normalization is additive, timestamp-preserving, repeatable, and rolls back
  fully on failure; the CLI is read-only by default.
- Fixed canonical completion identities are independent of the converter.
  Every completed body is a failing sentinel; any pending suffix runs once in
  registry order, including synthetic future appends in non-lexical order.
  Retain fresh/previous-release database upgrade coverage.
- Scoped cleanup succeeds alongside unrelated debt; new debt fails even at equal
  total counts; unchanged owned debt still fails. Failed cleanup rolls back.
- Duplicate full IDs fail; equal or non-increasing numeric prefixes do not force
  renaming. Published identity, order, and body rewrite guards remain protected.
