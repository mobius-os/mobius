# Compatibility lifecycle

Compatibility code is allowed when it protects owner data or a real external
contract. Temporary rollout shims need an owner and an observable exit proof;
“one release” is not a permanent architecture.

## Upgrade floor

Möbius is continuously deployed from git — there are no numbered releases — so
the supported-upgrade window is a **rolling date**, not a version.

> **A self-hosted owner may upgrade directly from any commit landed in the last
> 90 days.** An install older than that must update through an intermediate
> checkout rather than jumping to HEAD.

This is the number that makes removals mechanical instead of a judgement call:

- A compatibility path may be deleted once it has been **superseded for 90 days
  AND** its exit proof in the table below holds. Both conditions, not either.
- Before that date, deleting it is a regression for a supported upgrade, no
  matter how empty the current database looks. Row counts on one instance are
  evidence about *that* instance, never about the upgrade window.
- After that date, keeping it needs a fresh justification recorded here.

“One release” is explicitly NOT the floor. Retiring the `mobius-open-tabs`
workspace fallback after a single release stranded owners who skipped a
version and restored an empty workspace; the 90-day window exists so that
cannot recur.

### Earliest removal dates

Every active migration below was introduced in July 2026, so **none is yet
eligible** — the correct action today is to leave all of them in place.

| Path | Superseded | Earliest removal |
| --- | --- | --- |
| legacy platform-app migration (`bootstrap.py`, `install.py`) | 2026-07-10 | 2026-10-08 |
| transcript `cid` backfill (`chat_writer.py`) | 2026-07-13 | 2026-10-11 |
| `appFrameStorage.js` per-app legacy scan | 2026-07-13 | 2026-10-11 |
| raw-bcrypt owner hash (`auth.py`) | 2026-07-21 | 2026-10-19 |
| notification target aliases (`push.py`) | 2026-07-28 | 2026-10-26 |

The July 2026 maintenance baseline retired two expired mirrors and one
status-derived fallback:

- `mobius-open-tabs`, the flat workspace read/write path. `mobius-workspace` is
  now the only workspace state.
- `mobius-theme-bg`, the bare-colour theme read/write path. The structured
  `mobius-theme` value is now the only cold-boot theme state.
- status-derived AskUserQuestion row ownership. `answer_turn` is now required at
  that semantic boundary.

## Active migrations

| Owner | Protected data or contract | Exit proof | Earliest removal |
| --- | --- | --- | --- |
| `chat_writer.py` transcript migration | Historical chats without stable `cid` values or bounded thinking sidecars | The versioned migration ledger records the transcript migration and production diagnostics report no unmigrated rows | Remove only the read fallbacks in the next transcript-format change after that proof; keep the migrated `legacy-*` identifiers as data |
| `auth.py` / `routes/auth.py` | Owner password hashes written in the earlier raw-bcrypt form | A versioned migration or bounded audit proves every owner hash uses the current wrapper format | Remove raw-bcrypt verification in the following release |
| `install.py`, `routes/apps.py`, `compiler.py` | Installed apps created before source directories, immutable bundles, capability contracts, or publication records existed | Startup migrations and an app-table audit prove no installed row lacks the current source, bundle, contract, and publication identities | Remove each fallback with the migration that establishes its corresponding non-null invariant |
| `appFrameStorage.js` | Preferences written by the former same-origin app frame, including the narrow CubeRun and Tandem allowlists | **No code-level proof exists — the "copy marker" this row used to cite was never implemented, so the condition was unsatisfiable and the scan was permanent by accident.** The upgrade floor is the proof: past the date below, no supported install can still hold unmigrated same-origin keys | Remove the per-app legacy scans, and the `LEGACY_KEYS_BY_SLUG` allowlist naming individual apps with them, on 2026-10-11 |
| provider event translators | Saved or replayed Claude/Codex events from older SDK payload shapes | The pinned provider SDK minimum and retained replay fixtures no longer emit or contain the old shape | Remove one fallback at a time with the SDK pin bump that makes it unreachable |
| old notification targets (`/app/:id`, `/chat/:id`) | Previously delivered push/email links outside the current `/shell/` route shape | Product policy defines an expiry longer than every notification/link retention window and access logs show no use | Remove the parser aliases after that dated window |

Permanent interoperability—standard GitHub status contexts, documented
third-party skill shapes, or public storage request formats—is not a temporary
migration and does not belong in this removal ledger.
