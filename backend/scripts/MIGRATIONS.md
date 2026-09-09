# Permanent migration contract

## One current design

- The full published ID identifies an operation; registry order determines
  execution. Never rename a shipped ID to reconcile numeric prefixes.
- Append genuinely new behavior under a new unique ID. Do not rewrite an
  already-applied body to introduce behavior that existing databases will skip.
- Startup uses exact completion IDs, not aliases, suffix matching, or code hashes.
- One runner owns execution. Bodies remain retry-safe because body commits and
  ledger inserts are separate. Recorded completions do not replay; this is not
  a crash-proof exactly-once transaction across database and filesystem effects.
- Convert old data explicitly when needed, then use the current design. Keep
  incident-specific conversion outside startup; never grow a compatibility layer
  to accommodate routine source reconciliation.

## Validation belongs to the change

A migration must establish its owned postconditions without introducing new
integrity debt. Unrelated pre-existing debt is not a reason to reject an upgrade.
Do not turn every migration into a global audit. Where before/after comparison
is necessary, compare violation identities, not just counts, in one transaction;
verify that identities remain meaningful if tables or foreign keys change.

Keep physical database integrity checks and broader debt audits in deployment
preflight. Test both fresh and existing databases: create_all does not upgrade
existing columns. Clone validation must isolate filesystem destinations as well
as the database, and inspect pending bodies for paths stored in database rows.
Obtain a consistent, validated backup before an approved live cutover. A task
approval is not approval to restart the running service.

## Tests should outlive the next migration

Keep historical ledger fixtures and their expected completed identities fixed
and independent of the converter under test. Reject calls to completed bodies;
require every genuinely pending body to run once in registry order, followed by
a no-op second pass. Future appends must not require expanding an incident-test
allowlist. Retain rollback/retry tests and published-history checks.

## Historical recovery procedures

[The 2026-09-08 cutover](MIGRATION-CUTOVER.md) documents the finite ledger
normalization and approved retention-audit correction. Its offline converter is
frozen recovery support for that history, not a pattern for future renames.
