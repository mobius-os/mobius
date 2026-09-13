# Compatibility lifecycle

Möbius preserves **owner data and explicit external contracts**, not obsolete
runtime behavior. The current runtime should have one request shape, one state
model, and one execution path.

## Fix-forward rules

- Normalize durable owner data before current code consumes it. A migration may
  understand an old shape; request handlers, UI, workers, and recovery code do
  not keep serving it.
- Make migrations one-way, idempotent, and self-contained. Write their durable
  receipt only after the new invariant is proven. An interrupted migration must
  safely retry.
- Fail closed when a safe conversion cannot be inferred. Never guess a model,
  rewrite local Git history, resume old work, or discard an unknown future
  shape merely to make an upgrade appear seamless.
- Keep published schema-migration identities, order, and owned code immutable.
  Append a new migration instead of editing history.
- Preserve an old executable path only when a real independently deployed
  counterparty still has a documented contract with it. Version that contract
  at its owning boundary; do not disguise it as an internal fallback.

Elapsed time, release count, traffic on one instance, and an empty table on one
instance are not cutover proofs. A proof is an invariant the code can verify:
a schema-ledger entry, a content-addressed receipt, or an exact durable marker.

## Upgrade checkpoints

Möbius deploys continuously from Git rather than numbered releases. When a
change cannot both normalize old data and delete unsafe old behavior in one
boot, the normalizing commit is an explicit **upgrade checkpoint**:

1. the checkpoint boots, performs the one-way migration, and records proof;
2. the later runtime requires that proof and otherwise fails closed with the
   exact checkpoint the owner must traverse;
3. the later change removes the old executable machinery while retaining owner
   data and published migration history.

This is a causal barrier, not a waiting period. Owners upgrading from before a
retired checkpoint may need to run the checkpoint once before jumping to HEAD.

The current compatibility-deletion programme owns these checkpoints:

| Cutover | Proof | Current-only behavior after proof |
| --- | --- | --- |
| Scheduled app jobs | every boot scans every accepted runtime pointer, including tombstones, and repairs any legacy job declaration it finds | jobs require an absolute shebang and executable declaration; no Bash or chmod fallback |
| `mind` → `memory`, `dreaming` → `reflection` | schema ledger `0054_retired_app_identities` proves the configured database; every image boot discards stale filesystem proof, scans files/logs/skills/crontab, then republishes `/data/.migration-receipts/app-identity-files-v1` for that boot | only current app identities are recognized |
| activation marker v0/v1 → v2 | every boot inspects the current marker and writes `/data/.platform-activation-v2` only after the v2 parser accepts it (or proves no marker exists) | the runtime parser accepts v2 only |
| Gauntlet shutdown | `gauntlet_target_mutex.id = -1` after exact legacy lineages are stopped and detached from generic recovery | the stacked deletion may remove Gauntlet models, writer commands, startup and recovery logic while leaving historical tables inert |
| retained notification links | schema ledger `0052_compatibility_cutover_data` after DB targets/actions are structurally rewritten without changing absolute-link origins | current `/shell/` app/chat targets only; no retired route parser |
| legacy Project copies and inferred Creations | schema ledger `0055_declarative_project_artifacts` after independent roots are detached from retired source-management metadata and retained preview/artifact choices are stored explicitly | only `management=linked` declares live source ownership; preview source, builder, type, transport and output are data rather than runtime guesses |
| background-agent provider choices | the atomically stored `background_agents.providers` shape itself; startup rechecks it on every boot, restoring `primary`/`fallback` reopens the cutover, and filesystem read failures fail startup closed | stored provider rows are authoritative; old mirrors are removed and never merged back |

## Earned compatibility

The following complexity remains because it protects data or a genuine
contract:

- append-only schema migration history and its ledger;
- raw-bcrypt password verification until that owner's successful login or an
  explicit password migration proves the current wrapper format;
- transcript/provider-event readers needed for durable historical chat content,
  until a versioned migration proves every retained row uses the current shape;
- documented public interoperability such as GitHub protocols, skill formats,
  and public storage request formats.
- the plural provider-status response's `authenticated` alias until released
  first-party apps migrate to `configured`; remove it only in the later stacked
  platform deletion, without restoring the retired singular status route.

Everything else needs a named owner, a concrete protected contract, and a
machine-checkable exit proof. “Older code might call this” is not sufficient.
