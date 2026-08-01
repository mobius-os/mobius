# Compatibility lifecycle

Compatibility code is allowed when it protects owner data or a real external
contract. Temporary rollout shims need an owner and an observable exit proof;
“one release” is not a permanent architecture.

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
| `appFrameStorage.js` | Preferences written by the former same-origin app frame, including the narrow CubeRun and Tandem allowlists | Each affected installation records a successful copy into its scoped storage prefix | Remove per-app legacy scans after the copy marker has shipped for one release |
| provider event translators | Saved or replayed Claude/Codex events from older SDK payload shapes | The pinned provider SDK minimum and retained replay fixtures no longer emit or contain the old shape | Remove one fallback at a time with the SDK pin bump that makes it unreachable |
| old notification targets (`/app/:id`, `/chat/:id`) | Previously delivered push/email links outside the current `/shell/` route shape | Product policy defines an expiry longer than every notification/link retention window and access logs show no use | Remove the parser aliases after that dated window |

Permanent interoperability—standard GitHub status contexts, documented
third-party skill shapes, or public storage request formats—is not a temporary
migration and does not belong in this removal ledger.
